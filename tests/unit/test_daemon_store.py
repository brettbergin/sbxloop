"""DaemonStore: work-item bookkeeping the outer loop depends on."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.store import StateStore


def item(key: str = "7", **overrides: object) -> WorkItem:
    fields: dict[str, object] = {
        "item_id": f"gh:{key}",
        "source": "github",
        "source_key": key,
        "title": f"issue {key}",
        "body": "do the thing",
        "url": f"https://github.com/o/r/issues/{key}",
    }
    fields.update(overrides)
    return WorkItem(**fields)  # type: ignore[arg-type]


class TestUpsert:
    def test_first_upsert_is_new_then_dedups(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        assert store.upsert_new(item(), now=100.0) is True
        assert store.upsert_new(item(), now=101.0) is False
        got = store.get("gh:7")
        assert got is not None and got.state == "queued" and got.created_at == 100.0

    def test_finished_row_superseded_only_when_content_changed(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_done("gh:7", now=2.0)
        # identical re-discovery of a done item is NOT new work
        assert store.upsert_new(item(), now=3.0) is False
        # edited content is
        assert store.upsert_new(item(body="do MORE things"), now=4.0) is True
        got = store.get("gh:7")
        assert got is not None and got.state == "queued" and got.body == "do MORE things"

    def test_running_row_never_superseded(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:7", "r1", now=2.0)
        assert store.upsert_new(item(body="changed"), now=3.0) is False


class TestQueueAndAttempts:
    def test_fifo_and_backoff(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item("1"), now=10.0)
        store.upsert_new(item("2"), now=20.0)
        assert store.next_queued(now=30.0, backoff_s=100.0).item_id == "gh:1"  # type: ignore[union-attr]
        # item 1 fails once at t=40 → eligible again at 40 + 1*100
        store.mark_running("gh:1", "r1", now=35.0)
        store.mark_failed("gh:1", "boom", now=40.0, requeue=True)
        assert store.next_queued(now=50.0, backoff_s=100.0).item_id == "gh:2"  # type: ignore[union-attr]
        store.mark_running("gh:2", "r2", now=55.0)
        store.mark_done("gh:2", now=60.0)
        assert store.next_queued(now=100.0, backoff_s=100.0) is None
        assert store.next_queued(now=140.0, backoff_s=100.0).item_id == "gh:1"  # type: ignore[union-attr]

    def test_same_created_at_dispatches_in_insertion_order(self, tmp_path: Path) -> None:
        """A batch upserted with one `now` must still be FIFO: rowid breaks
        the created_at tie (review: SQL guarantees no order for equal keys)."""
        store = DaemonStore(tmp_path / "state.db")
        for key in ("c", "a", "b"):  # deliberately not sorted by key
            store.upsert_new(item(key), now=50.0)
        order = []
        while (nxt := store.next_queued(now=60.0, backoff_s=1.0)) is not None:
            order.append(nxt.source_key)
            store.mark_running(nxt.item_id, f"r{nxt.source_key}", now=61.0)
        assert order == ["c", "a", "b"]

    def test_mark_running_unknown_item_leaves_no_orphan_ledger_row(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        with pytest.raises(KeyError):
            store.mark_running("gh:nope", "r_orphan", now=1.0)
        assert store.runs_started_since(0) == 0

    def test_attempt_counting_requeue_vs_abandon(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:7", "r1", now=2.0)
        got = store.get("gh:7")
        assert got is not None and got.attempts == 1 and got.run_id == "r1"
        store.mark_failed("gh:7", "err1", now=3.0, requeue=True)
        assert store.get("gh:7").state == "queued"  # type: ignore[union-attr]
        store.mark_running("gh:7", "r2", now=4.0)
        store.mark_failed("gh:7", "err2", now=5.0, requeue=False)
        got = store.get("gh:7")
        assert got is not None
        assert got.state == "abandoned" and got.attempts == 2 and got.last_error == "err2"

    def test_cancelled_is_terminal_and_retry_resets_the_attempt_budget(
        self, tmp_path: Path
    ) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:7", "r1", now=2.0)
        store.mark_cancelled("gh:7", "cancelled by op", now=3.0)
        got = store.get("gh:7")
        assert got is not None and got.state == "cancelled" and got.last_error == "cancelled by op"
        assert store.next_queued(now=1e9, backoff_s=1) is None  # never auto-retried
        assert store.upsert_new(item(), now=4.0) is False  # re-discovery dedups like done
        with pytest.raises(ValueError, match="use retry"):
            store.requeue("gh:7", now=5.0)  # requeue is for running/queued items
        store.retry("gh:7", now=5.0, reason="re-queued by op")
        got = store.get("gh:7")
        assert got is not None and got.state == "queued" and got.attempts == 0
        assert got.last_error == "re-queued by op"
        # Cancel keeps the run for `sbxloop resume`; a re-queue runs fresh, so
        # the pin must go or the next tick would resume the cancelled run.
        assert got.run_id is None
        # A human's re-queue is eligible right away, no failure backoff.
        assert store.next_queued(now=5.0, backoff_s=900) is not None

    def test_running_items_and_unstarted_requeue(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_claimed("gh:7", now=1.5)
        store.mark_running("gh:7", "r1", now=2.0)
        assert [i.item_id for i in store.running_items()] == ["gh:7"]
        store.mark_requeued_unstarted("gh:7", now=3.0)
        got = store.get("gh:7")
        assert got is not None
        assert got.state == "queued" and got.claimed is True and got.run_id is None


class TestResumeAndBreaker:
    def test_resume_pending_goes_first_and_skips_backoff(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item("1"), now=1.0)
        store.upsert_new(item("2"), now=2.0)
        store.mark_claimed("gh:2", now=2.0)
        store.mark_running("gh:2", "r2", now=3.0)  # attempts -> 1
        store.mark_resume_pending("gh:2", now=4.0)
        # gh:1 is older and has no backoff, but the interrupted run is
        # in-flight work and goes first — regardless of gh:2's backoff.
        got = store.next_queued(now=4.0, backoff_s=900.0)
        assert got is not None and got.item_id == "gh:2" and got.run_id == "r2"

    def test_mark_resuming_records_resume_and_keeps_attempts(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_claimed("gh:7", now=1.0)
        store.mark_running("gh:7", "r1", now=2.0)
        store.mark_resume_pending("gh:7", now=3.0)
        store.mark_resuming("gh:7", "r1", now=4.0)
        got = store.get("gh:7")
        assert got is not None and got.state == "running" and got.attempts == 1
        assert store.resumes_for_item("gh:7") == 1
        assert store.resumes_since(3.5) == 1 and store.resumes_since(4.5) == 0
        # The daily cap counts fresh starts AND resumes.
        assert store.runs_started_since(0) == 2
        with pytest.raises(KeyError):
            store.mark_resuming("gh:7", "other-run", now=5.0)

    def test_requeue_after_failure_unpins_the_run(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:7", "r1", now=2.0)
        store.mark_failed("gh:7", "boom", now=3.0, requeue=True)
        got = store.get("gh:7")
        assert got is not None and got.state == "queued" and got.run_id is None
        store.mark_running("gh:7", "r2", now=4.0)
        store.mark_failed("gh:7", "boom", now=5.0, requeue=False)
        assert store.get("gh:7").run_id == "r2"  # type: ignore[union-attr]

    def test_item_kind_roundtrips_and_defaults_to_patch(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(
            WorkItem(item_id="gh:9", source="github", source_key="9", kind="audit", title="A"),
            now=1.0,
        )
        store.upsert_new(
            WorkItem(item_id="gh:1", source="github", source_key="1", title="P"), now=1.0
        )
        assert store.get("gh:9").kind == "audit"  # type: ignore[union-attr]
        assert store.get("gh:1").kind == "patch"  # type: ignore[union-attr]

    def test_breaker_roundtrip(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        assert store.breaker() == (None, 0)
        store.set_breaker(1234.5, 3)
        assert store.breaker() == (1234.5, 3)
        store.set_breaker(None, 1)
        assert DaemonStore(tmp_path / "state.db").breaker() == (None, 1)

    def test_generic_state_values(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        assert store.get_value("concierge_session_id") is None
        store.set_value("concierge_session_id", "sess-1")
        assert DaemonStore(tmp_path / "state.db").get_value("concierge_session_id") == "sess-1"
        store.set_value("concierge_session_id", None)
        assert store.get_value("concierge_session_id") is None
        # coexists with the breaker rows on the same table
        store.set_breaker(1.0, 2)
        store.set_value("k", "v")
        assert store.breaker() == (1.0, 2) and store.get_value("k") == "v"


class TestLedgerBacklogThreads:
    def test_rolling_window(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        # runs at t=1000, t=50000, t=90000; a 24h window ending at 90000
        # starts at 3600 — the first run has aged out, two remain
        for i, ts in enumerate((1000.0, 50000.0, 90000.0)):
            store.upsert_new(item(str(i)), now=ts)
            store.mark_running(f"gh:{i}", f"r{i}", now=ts)
        assert store.runs_started_since(90000.0 - 86400) == 2
        assert store.runs_started_since(0) == 3
        store.finish_ledger("r0", "done", now=1500.0)

    def test_item_for_run(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item("5"), now=1.0)
        store.mark_running("gh:5", "r5", now=2.0)
        assert store.item_for_run("r5") == "gh:5"
        assert store.item_for_run("r-unknown") is None

    def test_backlog_fingerprint_dedup(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        assert store.backlog_seen("abc") is False
        store.backlog_record("abc", "r1", "gh:9", now=1.0)
        assert store.backlog_seen("abc") is True

    def test_discord_thread_map_roundtrip(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        assert store.discord_thread("r1") is None
        store.record_discord_thread("r1", channel_id=11, thread_id=22, headline_id=33)
        assert store.discord_thread("r1") == (11, 22, 33, None)
        store.set_discord_status_id("r1", 44)
        assert store.discord_thread("r1").status_id == 44  # type: ignore[union-attr]
        assert store.run_for_thread(22) == "r1"
        assert store.run_for_thread(99) is None

    def test_coexists_with_statestore_on_one_db(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        engine_store = StateStore(db)
        daemon_store = DaemonStore(db)
        engine_store.create_run("r1", "outcome")
        daemon_store.upsert_new(item(), now=1.0)
        daemon_store.mark_running("gh:7", "r1", now=2.0)
        assert engine_store.get_run("r1").outcome == "outcome"
        assert daemon_store.get("gh:7").run_id == "r1"  # type: ignore[union-attr]
        engine_store.close()
        daemon_store.close()


class TestOperatorControls:
    """#229: abandon / retry / requeue transitions and what they clear."""

    def test_abandon_keeps_run_id_and_records_reason(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:7", "r1", now=2.0)
        got = store.abandon("gh:7", "spiraling plan", now=3.0)
        assert got.state == "abandoned" and got.run_id == "r1"
        assert got.last_error == "spiraling plan"
        with pytest.raises(ValueError, match="already abandoned"):
            store.abandon("gh:7", "again", now=4.0)
        with pytest.raises(KeyError):
            store.abandon("gh:404", "x", now=4.0)

    def test_retry_resets_attempts_and_unpins_run(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:7", "r1", now=2.0)
        store.mark_failed("gh:7", "boom", now=3.0, requeue=False)
        got = store.retry("gh:7", now=4.0)
        assert (got.state, got.attempts, got.run_id, got.last_error) == ("queued", 0, None, None)
        assert got.claimed is False  # untouched: whatever the source-side claim was stays
        # eligible immediately (no backoff): attempts are zero
        assert store.next_queued(now=4.0, backoff_s=600.0) is not None

    def test_retry_refuses_running_and_done(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:7", "r1", now=2.0)
        with pytest.raises(ValueError, match="abandon it first"):
            store.retry("gh:7", now=3.0)
        store.mark_done("gh:7", now=3.0)
        with pytest.raises(ValueError, match="is done"):
            store.retry("gh:7", now=4.0)

    def test_requeue_keeps_attempts_but_clears_run(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:7", "r1", now=2.0)
        got = store.requeue("gh:7", now=3.0)
        assert (got.state, got.attempts, got.run_id) == ("queued", 1, None)
        assert store.running_items() == []
        store.mark_failed("gh:7", "x", now=4.0, requeue=False)
        with pytest.raises(ValueError, match="use retry"):
            store.requeue("gh:7", now=5.0)

    def test_items_lists_all_or_filtered(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item("1"), now=1.0)
        store.upsert_new(item("2"), now=2.0)
        store.mark_running("gh:2", "r2", now=3.0)
        store.mark_failed("gh:2", "boom", now=4.0, requeue=False)
        assert [i.item_id for i in store.items()] == ["gh:1", "gh:2"]
        assert [i.item_id for i in store.items(["abandoned"])] == ["gh:2"]
        assert [i.item_id for i in store.items(["queued", "abandoned"])] == ["gh:1", "gh:2"]
        assert store.items(["done"]) == []

    def test_transitions_are_conditional_against_a_concurrent_settle(self, tmp_path: Path) -> None:
        """The CLI is another process: a daemon settling the item on its own
        connection must win. The state check and the update are one
        statement (``WHERE state IN``), so a command based on a stale view
        is refused with the state that is actually there and never
        overwrites the verdict."""
        store = DaemonStore(tmp_path / "state.db")
        other = DaemonStore(tmp_path / "state.db")  # the daemon's connection
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:7", "r1", now=2.0)
        stale = store.get("gh:7")
        assert stale is not None and stale.state == "running"  # what the CLI saw
        other.mark_done("gh:7", now=3.0)  # the daemon settles it meanwhile
        with pytest.raises(ValueError, match="already done"):
            store.abandon("gh:7", "stale", now=4.0)
        with pytest.raises(ValueError, match="is done"):
            store.requeue("gh:7", now=4.0)
        with pytest.raises(ValueError, match="is done"):
            store.retry("gh:7", now=4.0)
        assert other.get("gh:7").state == "done"  # type: ignore[union-attr]
        other.close()

    def test_abandon_and_retry_owe_the_source_a_report(self, tmp_path: Path) -> None:
        """A row-only CLI abandon/retry cannot report; the row carries the
        debt until the loop pays it, once."""
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        assert store.pending_reports() == []
        assert store.abandon("gh:7", "nope", now=2.0).pending_report == "abandoned"
        assert [i.item_id for i in store.pending_reports()] == ["gh:7"]
        assert store.take_pending_report("gh:7") is True
        assert store.take_pending_report("gh:7") is False  # paid: exactly once
        assert store.pending_reports() == []
        assert store.retry("gh:7", now=3.0).pending_report == "requeued"
        assert store.get("gh:7").updated_at == 3.0  # type: ignore[union-attr]
        store.take_pending_report("gh:7")
        # delivery is not an item change: the backoff clock does not move
        assert store.get("gh:7").updated_at == 3.0  # type: ignore[union-attr]
        # requeue (unpin) tells the source nothing
        store.mark_running("gh:7", "r1", now=4.0)
        assert store.requeue("gh:7", now=5.0).pending_report is None


class TestMergeWatchRows:
    """The merge watch's bookkeeping: which rows are due, and what settles
    them. The rows are the restart story — no memory is involved."""

    def _armed(self, tmp_path: Path) -> DaemonStore:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_done("gh:7", now=2.0)
        store.record_delivery("gh:7", 9, "sbxloop/r1", 2.0, url="https://x/pull/9")
        return store

    def test_record_delivery_stores_the_url_and_rearms_the_watch(self, tmp_path: Path) -> None:
        store = self._armed(tmp_path)
        state = store.pr_state("gh:7")
        assert state is not None and state.pr_url == "https://x/pull/9"
        assert not state.settled and state.merged_at is None
        store.settle_merge("gh:7", 3.0, merged_at=3.0)
        # A re-delivery (fix round) means there is a PR to watch again.
        store.record_delivery("gh:7", 9, "sbxloop/r2", 4.0, url="https://x/pull/9")
        state = store.pr_state("gh:7")
        assert state is not None and not state.settled and state.merged_at is None

    def test_merge_watch_returns_only_done_unsettled_due_rows(self, tmp_path: Path) -> None:
        store = self._armed(tmp_path)
        assert store.merge_watch(1000.0, 300.0) == [("gh:7", 9, "https://x/pull/9")]
        # Throttled: a fresh check pushes it past the interval.
        store.touch_merge_check("gh:7", 1000.0)
        assert store.merge_watch(1200.0, 300.0) == []
        assert store.merge_watch(1301.0, 300.0) != []
        # Settled rows never come back.
        store.settle_merge("gh:7", 1400.0, merged_at=1400.0)
        assert store.merge_watch(9999.0, 300.0) == []
        state = store.pr_state("gh:7")
        assert state is not None and state.settled and state.merged_at == 1400.0

    def test_merge_watch_skips_items_not_done(self, tmp_path: Path) -> None:
        """A reviewing item is the acceptance gate's to advance, and a
        failed one has been handed to a human — neither costs a read."""
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.record_delivery("gh:7", 9, "b", 1.0, url="u")
        store.mark_reviewing("gh:7", now=2.0)
        assert store.merge_watch(1000.0, 300.0) == []
        store.mark_failed("gh:7", "boom", 3.0, requeue=False)
        assert store.merge_watch(1000.0, 300.0) == []
        store.set_state("gh:7", "done", 4.0)
        assert store.merge_watch(1000.0, 300.0) == [("gh:7", 9, "u")]

    def test_pre_migration_rows_are_swept_retroactively(self, tmp_path: Path) -> None:
        """settled defaults to 0, so rows from before the columns existed —
        accepted items whose PRs merged with nobody watching — come due on
        the first tick after the upgrade. Opening the store twice also
        proves the migrations are idempotent."""
        self._armed(tmp_path)
        again = DaemonStore(tmp_path / "state.db")
        assert again.merge_watch(1000.0, 300.0) == [("gh:7", 9, "https://x/pull/9")]
