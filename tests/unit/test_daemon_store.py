"""DaemonStore: work-item bookkeeping the outer loop depends on."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import LEGACY_SUFFIX, SCHEMA_VERSION, DaemonStore
from sbxloop.engine.store import StateStore
from sbxloop.errors import DaemonError


def item(key: str = "7", **overrides: object) -> WorkItem:
    fields: dict[str, object] = {
        "item_id": f"gh:{key}",
        "source_key": key,
        "title": f"issue {key}",
        "body": "do the thing",
        "url": f"https://github.com/o/r/issues/{key}",
    }
    fields.update(overrides)
    return WorkItem(**fields)  # type: ignore[arg-type]


def _pre_multirepo_db(tmp_path: Path) -> Path:
    """A state.db in the shape sbxloop wrote before multi-repo support."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE daemon_work_items (item_id TEXT PRIMARY KEY, "
        "source_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, "
        "body TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', "
        "state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
        "claimed INTEGER NOT NULL DEFAULT 0, run_id TEXT, last_error TEXT, "
        "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
        "pending_report TEXT, requested_by TEXT)"
    )
    conn.execute(
        "CREATE TABLE daemon_requesters (source_key TEXT PRIMARY KEY, "
        "requester_id TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    conn.commit()
    conn.close()
    return path


class TestUpsert:
    def test_first_upsert_is_new_then_dedups(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        assert store.upsert_new(item(), now=100.0) is True
        assert store.upsert_new(item(), now=101.0) is False
        got = store.get("gh:issue:7")
        assert got is not None and got.state == "queued" and got.created_at == 100.0

    def test_finished_row_superseded_only_when_content_changed(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_done("gh:issue:7", now=2.0)
        # identical re-discovery of a done item is NOT new work
        assert store.upsert_new(item(), now=3.0) is False
        # edited content is
        assert store.upsert_new(item(body="do MORE things"), now=4.0) is True
        got = store.get("gh:issue:7")
        assert got is not None and got.state == "queued" and got.body == "do MORE things"

    def test_blocked_row_is_terminal_for_dedup(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_blocked("gh:issue:7", "405", now=2.0)
        assert store.upsert_new(item(), now=3.0) is False
        assert store.upsert_new(item(body="edited"), now=4.0) is True

    def test_running_row_never_superseded(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        assert store.upsert_new(item(body="changed"), now=3.0) is False

    def test_requester_recorded_by_the_concierge_lands_on_the_item(self, tmp_path: Path) -> None:
        """The concierge files the issue and knows who asked; discovery
        builds the item later from GitHub, which does not — the store
        joins the two, and the id never appears in the public issue."""
        store = DaemonStore(tmp_path / "state.db")
        store.note_requester("7", "1234567890", now=0.5)
        store.upsert_new(item(), now=1.0)
        got = store.get("gh:issue:7")
        assert got is not None and got.requested_by == "1234567890"
        # An item the source already attributed keeps its own attribution.
        store.note_requester("8", "999", now=0.5)
        store.upsert_new(item("8", requested_by="111"), now=1.0)
        assert store.get("gh:issue:8").requested_by == "111"  # type: ignore[union-attr]
        assert store.get("gh:issue:7").requested_by == "1234567890"  # type: ignore[union-attr]
        # Nothing recorded → nothing attributed.
        store.upsert_new(item("9"), now=1.0)
        assert store.get("gh:issue:9").requested_by is None  # type: ignore[union-attr]


class TestQueueAndAttempts:
    def test_fifo_and_backoff(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item("1"), now=10.0)
        store.upsert_new(item("2"), now=20.0)
        assert store.next_queued(now=30.0, backoff_s=100.0).item_id == "gh:issue:1"  # type: ignore[union-attr]
        # item 1 fails once at t=40 → eligible again at 40 + 1*100
        store.mark_running("gh:issue:1", "r1", now=35.0)
        store.mark_failed("gh:issue:1", "boom", now=40.0, requeue=True)
        assert store.next_queued(now=50.0, backoff_s=100.0).item_id == "gh:issue:2"  # type: ignore[union-attr]
        store.mark_running("gh:issue:2", "r2", now=55.0)
        store.mark_done("gh:issue:2", now=60.0)
        assert store.next_queued(now=100.0, backoff_s=100.0) is None
        assert store.next_queued(now=140.0, backoff_s=100.0).item_id == "gh:issue:1"  # type: ignore[union-attr]

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

    def test_attempt_counting_requeue_vs_failed(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        got = store.get("gh:issue:7")
        assert got is not None and got.attempts == 1 and got.run_id == "r1"
        store.mark_failed("gh:issue:7", "err1", now=3.0, requeue=True)
        assert store.get("gh:issue:7").state == "queued"  # type: ignore[union-attr]
        store.mark_running("gh:issue:7", "r2", now=4.0)
        store.mark_failed("gh:issue:7", "err2", now=5.0, requeue=False)
        got = store.get("gh:issue:7")
        assert got is not None
        assert got.state == "failed" and got.attempts == 2 and got.last_error == "err2"

    def test_cancelled_is_terminal_and_retry_resets_the_attempt_budget(
        self, tmp_path: Path
    ) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        store.mark_cancelled("gh:issue:7", "cancelled by op", now=3.0)
        got = store.get("gh:issue:7")
        assert got is not None and got.state == "cancelled" and got.last_error == "cancelled by op"
        assert store.next_queued(now=1e9, backoff_s=1) is None  # never auto-retried
        assert store.upsert_new(item(), now=4.0) is False  # re-discovery dedups like done
        with pytest.raises(ValueError, match="use retry"):
            store.requeue("gh:issue:7", now=5.0)  # requeue is for running/queued items
        store.retry("gh:issue:7", now=5.0, reason="re-queued by op")
        got = store.get("gh:issue:7")
        assert got is not None and got.state == "queued" and got.attempts == 0
        assert got.last_error == "re-queued by op"
        # Cancel keeps the run for `sbxloop resume`; a re-queue runs fresh, so
        # the pin must go or the next tick would resume the cancelled run.
        assert got.run_id is None
        # A human's re-queue is eligible right away, no failure backoff.
        assert store.next_queued(now=5.0, backoff_s=900) is not None

    def test_blocked_is_terminal_until_a_human_retries(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        store.mark_blocked("gh:issue:7", "GitHub refused the merge", now=3.0)
        got = store.get("gh:issue:7")
        assert got is not None and got.state == "blocked" and got.run_id == "r1"
        assert got.last_error == "GitHub refused the merge"
        assert got.pending_report == "blocked"  # the source is owed the label
        assert store.next_queued(now=1e9, backoff_s=1) is None
        assert [i.item_id for i in store.items(["blocked"])] == ["gh:issue:7"]
        with pytest.raises(ValueError, match="use retry"):
            store.requeue("gh:issue:7", now=4.0)
        got = store.retry("gh:issue:7", now=5.0)
        assert got.state == "queued" and got.attempts == 0 and got.run_id is None
        assert got.pending_report == "requeued"

    def test_running_items_and_unstarted_requeue(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_claimed("gh:issue:7", now=1.5)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        assert [i.item_id for i in store.running_items()] == ["gh:issue:7"]
        store.mark_requeued_unstarted("gh:issue:7", now=3.0)
        got = store.get("gh:issue:7")
        assert got is not None
        assert got.state == "queued" and got.claimed is True and got.run_id is None


class TestResumeAndBreaker:
    def test_resume_pending_goes_first_and_skips_backoff(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item("1"), now=1.0)
        store.upsert_new(item("2"), now=2.0)
        store.mark_claimed("gh:issue:2", now=2.0)
        store.mark_running("gh:issue:2", "r2", now=3.0)  # attempts -> 1
        store.mark_resume_pending("gh:issue:2", now=4.0)
        # gh:issue:1 is older and has no backoff, but the interrupted run is
        # in-flight work and goes first — regardless of gh:issue:2's backoff.
        got = store.next_queued(now=4.0, backoff_s=900.0)
        assert got is not None and got.item_id == "gh:issue:2" and got.run_id == "r2"

    def test_mark_resuming_records_resume_and_keeps_attempts(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_claimed("gh:issue:7", now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        store.mark_resume_pending("gh:issue:7", now=3.0)
        store.mark_resuming("gh:issue:7", "r1", now=4.0)
        got = store.get("gh:issue:7")
        assert got is not None and got.state == "running" and got.attempts == 1
        assert store.resumes_for_item("gh:issue:7") == 1
        assert store.resumes_since(3.5) == 1 and store.resumes_since(4.5) == 0
        # The daily cap counts fresh starts AND resumes.
        assert store.runs_started_since(0) == 2
        with pytest.raises(KeyError):
            store.mark_resuming("gh:issue:7", "other-run", now=5.0)

    def test_requeue_after_failure_unpins_the_run(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        store.mark_failed("gh:issue:7", "boom", now=3.0, requeue=True)
        got = store.get("gh:issue:7")
        assert got is not None and got.state == "queued" and got.run_id is None
        store.mark_running("gh:issue:7", "r2", now=4.0)
        store.mark_failed("gh:issue:7", "boom", now=5.0, requeue=False)
        assert store.get("gh:issue:7").run_id == "r2"  # type: ignore[union-attr]

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
        assert store.get_value("schema_version") == SCHEMA_VERSION


class TestLedgerAndThreads:
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

    def test_item_for_run_and_runs_for_item(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item("5"), now=1.0)
        store.mark_running("gh:issue:5", "r5", now=2.0)
        assert store.item_for_run("r5") == "gh:issue:5"
        assert store.item_for_run("r-unknown") is None
        assert store.runs_for_item("gh:issue:5") == ["r5"]

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
        daemon_store.mark_running("gh:issue:7", "r1", now=2.0)
        assert engine_store.get_run("r1").outcome == "outcome"
        assert daemon_store.get("gh:issue:7").run_id == "r1"  # type: ignore[union-attr]
        engine_store.close()
        daemon_store.close()

    def test_no_lane_tables_exist(self, tmp_path: Path) -> None:
        """The self-filing lanes are gone with their bookkeeping."""
        store = DaemonStore(tmp_path / "state.db")
        tables = {
            str(r[0])
            for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert tables >= {"daemon_work_items", "daemon_runs", "daemon_requesters"}
        assert not tables & {
            "daemon_backlog_filed",
            "daemon_postmortems",
            "daemon_reviews",
            "daemon_pr_state",
            "daemon_audits",
        }


class TestOperatorControls:
    """#229: abandon / retry / requeue transitions and what they clear."""

    def test_abandon_keeps_run_id_and_records_reason(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        got = store.abandon("gh:issue:7", "spiraling plan", now=3.0)
        assert got.state == "failed" and got.run_id == "r1"
        assert got.last_error == "spiraling plan"
        with pytest.raises(ValueError, match="already failed"):
            store.abandon("gh:issue:7", "again", now=4.0)
        with pytest.raises(KeyError):
            store.abandon("gh:issue:404", "x", now=4.0)

    def test_abandon_of_a_blocked_item_is_allowed(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_blocked("gh:issue:7", "405", now=2.0)
        got = store.abandon("gh:issue:7", "not worth it", now=3.0)
        assert got.state == "failed" and got.pending_report == "abandoned"

    def test_retry_resets_attempts_and_unpins_run(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        store.mark_failed("gh:issue:7", "boom", now=3.0, requeue=False)
        got = store.retry("gh:issue:7", now=4.0)
        assert (got.state, got.attempts, got.run_id, got.last_error) == ("queued", 0, None, None)
        assert got.claimed is False  # untouched: whatever the source-side claim was stays
        # eligible immediately (no backoff): attempts are zero
        assert store.next_queued(now=4.0, backoff_s=600.0) is not None

    def test_retry_refuses_running_and_done(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        with pytest.raises(ValueError, match="abandon it first"):
            store.retry("gh:issue:7", now=3.0)
        store.mark_done("gh:issue:7", now=3.0)
        with pytest.raises(ValueError, match="is done"):
            store.retry("gh:issue:7", now=4.0)

    def test_requeue_keeps_attempts_but_clears_run(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        got = store.requeue("gh:issue:7", now=3.0)
        assert (got.state, got.attempts, got.run_id) == ("queued", 1, None)
        assert store.running_items() == []
        store.mark_failed("gh:issue:7", "x", now=4.0, requeue=False)
        with pytest.raises(ValueError, match="use retry"):
            store.requeue("gh:issue:7", now=5.0)

    def test_items_lists_all_or_filtered(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item("1"), now=1.0)
        store.upsert_new(item("2"), now=2.0)
        store.mark_running("gh:issue:2", "r2", now=3.0)
        store.mark_failed("gh:issue:2", "boom", now=4.0, requeue=False)
        assert [i.item_id for i in store.items()] == ["gh:issue:1", "gh:issue:2"]
        assert [i.item_id for i in store.items(["failed"])] == ["gh:issue:2"]
        assert [i.item_id for i in store.items(["queued", "failed"])] == [
            "gh:issue:1",
            "gh:issue:2",
        ]
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
        store.mark_running("gh:issue:7", "r1", now=2.0)
        stale = store.get("gh:issue:7")
        assert stale is not None and stale.state == "running"  # what the CLI saw
        other.mark_done("gh:issue:7", now=3.0)  # the daemon settles it meanwhile
        with pytest.raises(ValueError, match="already done"):
            store.abandon("gh:issue:7", "stale", now=4.0)
        with pytest.raises(ValueError, match="is done"):
            store.requeue("gh:issue:7", now=4.0)
        with pytest.raises(ValueError, match="is done"):
            store.retry("gh:issue:7", now=4.0)
        assert other.get("gh:issue:7").state == "done"  # type: ignore[union-attr]
        other.close()

    def test_abandon_and_retry_owe_the_source_a_report(self, tmp_path: Path) -> None:
        """A row-only CLI abandon/retry cannot report; the row carries the
        debt until the loop pays it, once."""
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        assert store.pending_reports() == []
        assert store.abandon("gh:issue:7", "nope", now=2.0).pending_report == "abandoned"
        assert [i.item_id for i in store.pending_reports()] == ["gh:issue:7"]
        assert store.take_pending_report("gh:issue:7") is True
        assert store.take_pending_report("gh:issue:7") is False  # paid: exactly once
        assert store.pending_reports() == []
        assert store.retry("gh:issue:7", now=3.0).pending_report == "requeued"
        assert store.get("gh:issue:7").updated_at == 3.0  # type: ignore[union-attr]
        store.take_pending_report("gh:issue:7")
        # delivery is not an item change: the backoff clock does not move
        assert store.get("gh:issue:7").updated_at == 3.0  # type: ignore[union-attr]
        # requeue (unpin) tells the source nothing
        store.mark_running("gh:issue:7", "r1", now=4.0)
        assert store.requeue("gh:issue:7", now=5.0).pending_report is None

    def test_a_merged_run_owes_the_close_until_it_lands(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        store.mark_done("gh:issue:7", now=3.0, pending_report="merged")
        got = store.get("gh:issue:7")
        assert got is not None and got.state == "done" and got.pending_report == "merged"
        assert [i.item_id for i in store.pending_reports()] == ["gh:issue:7"]
        assert store.take_pending_report("gh:issue:7") is True
        assert store.get("gh:issue:7").pending_report is None  # type: ignore[union-attr]
        # plain mark_done owes nothing
        store.upsert_new(item("8"), now=1.0)
        store.mark_done("gh:issue:8", now=2.0)
        assert store.get("gh:issue:8").pending_report is None  # type: ignore[union-attr]


class TestArchiveLegacy:
    """The 1.0 cutover: a pre-1.0 daemon state.db is moved aside, not
    migrated — automatically, because the daemon host deploys unattended."""

    @staticmethod
    def _legacy(path: Path) -> None:
        with sqlite3.connect(path) as conn:
            conn.executescript(
                """
                CREATE TABLE daemon_work_items (
                    item_id TEXT PRIMARY KEY, source TEXT, source_key TEXT, kind TEXT,
                    title TEXT, state TEXT
                );
                CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE daemon_pr_state (item_id TEXT PRIMARY KEY);
                INSERT INTO daemon_work_items VALUES ('gh:issue:1','github','1','patch','x','done');
                """
            )
        # WAL/SHM sidecars travel with the file.
        path.with_name(path.name + "-wal").write_bytes(b"")
        path.with_name(path.name + "-shm").write_bytes(b"")

    def test_legacy_file_is_renamed_with_its_sidecars(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        self._legacy(path)
        archived = DaemonStore.archive_legacy(path, clock=lambda: 1234.0)
        assert archived == tmp_path / f"state.db{LEGACY_SUFFIX}"
        assert archived.exists() and not path.exists()
        assert (tmp_path / f"state.db{LEGACY_SUFFIX}-wal").exists()
        assert (tmp_path / f"state.db{LEGACY_SUFFIX}-shm").exists()
        assert not path.with_name("state.db-wal").exists()
        # A fresh store then opens clean, at the current schema.
        store = DaemonStore(path)
        assert store.get("gh:issue:1") is None
        assert store.get_value("schema_version") == SCHEMA_VERSION
        # …and the archive is left alone on the next start.
        assert DaemonStore.archive_legacy(path) is None

    def test_a_second_archive_does_not_clobber_the_first(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        self._legacy(path)
        first = DaemonStore.archive_legacy(path, clock=lambda: 1234.0)
        self._legacy(path)
        second = DaemonStore.archive_legacy(path, clock=lambda: 5678.0)
        assert first is not None and second is not None and first != second
        assert second.name == f"state.db{LEGACY_SUFFIX}.5678"
        assert first.exists() and second.exists()

    def test_engine_only_and_current_files_are_untouched(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        assert DaemonStore.archive_legacy(path) is None  # no file
        StateStore(path).create_run("r1", "o")  # a CLI host's engine-only db
        assert DaemonStore.archive_legacy(path) is None
        assert path.exists()
        DaemonStore(path)  # now at schema 2
        assert DaemonStore.archive_legacy(path) is None
        assert StateStore(path).get_run("r1").outcome == "o"

    def test_opening_a_legacy_file_directly_is_refused_clearly(self, tmp_path: Path) -> None:
        """`sbxloop daemon items` before the daemon's first 1.0 start must
        say what to do, not fail on a missing column."""
        path = tmp_path / "state.db"
        self._legacy(path)
        with pytest.raises(DaemonError, match=r"pre-1\.0"):
            DaemonStore(path)
        assert path.exists()  # nothing was moved by the refusal


class TestLegacyItemIds:
    """A store written before the typed-id migration keeps resolving."""

    @staticmethod
    def _write_legacy_row(path: Path, item_id: str = "gh:1234") -> None:
        """Insert a row exactly as the pre-migration daemon wrote it."""
        store = DaemonStore(path)
        store.close()
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO daemon_work_items (item_id, source_key, title, body, url, state, "
            "attempts, claimed, run_id, last_error, created_at, updated_at) "
            "VALUES (?, '1234', 'legacy', '', '', 'queued', 0, 0, NULL, NULL, 1.0, 1.0)",
            (item_id,),
        )
        conn.execute(
            "INSERT INTO daemon_runs (run_id, item_id, started_at) VALUES ('r9', ?, 1.0)",
            (item_id,),
        )
        conn.commit()
        conn.close()

    def test_legacy_row_resolves_under_both_forms(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        self._write_legacy_row(db)
        store = DaemonStore(db)
        for lookup in ("gh:1234", "gh:issue:1234"):
            got = store.get(lookup)
            assert got is not None, lookup
            # loaded rows surface in the typed form regardless of storage
            assert got.item_id == "gh:issue:1234"
        assert [i.item_id for i in store.items()] == ["gh:issue:1234"]
        assert [i.item_id for i in store.queued()] == ["gh:issue:1234"]

    def test_legacy_row_transitions_under_the_typed_form(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        self._write_legacy_row(db)
        store = DaemonStore(db)
        store.mark_running("gh:issue:1234", "r1", now=2.0)
        got = store.get("gh:1234")
        assert got is not None and got.state == "running" and got.run_id == "r1"
        assert store.abandon("gh:issue:1234", "nope", now=3.0).state == "failed"
        assert store.take_pending_report("gh:1234") is True
        assert store.retry("gh:1234", now=4.0).state == "queued"

    def test_legacy_ledger_rows_resolve_and_normalise(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        self._write_legacy_row(db)
        store = DaemonStore(db)
        assert store.runs_for_item("gh:issue:1234") == ["r9"]
        assert store.runs_for_item("gh:1234") == ["r9"]
        assert store.item_for_run("r9") == "gh:issue:1234"
        assert store.unsettled_runs() == [("r9", "gh:issue:1234")]

    def test_new_rows_are_written_typed(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        store = DaemonStore(db)
        # even an item constructed with the legacy spelling stores typed
        store.upsert_new(item(), now=1.0)
        with sqlite3.connect(db) as conn:
            stored = [r[0] for r in conn.execute("SELECT item_id FROM daemon_work_items")]
        assert stored == ["gh:issue:7"]


class TestRepoScoping:
    """Item identity is (issue number, repository) since multi-repo."""

    def test_same_issue_number_in_two_repos_is_two_items(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        a = item("4", item_id="gh:o/a:issue:4", repo="o/a")
        b = item("4", item_id="gh:o/b:issue:4", repo="o/b")
        assert store.upsert_new(a, now=1.0) is True
        assert store.upsert_new(b, now=1.0) is True
        assert store.upsert_new(a, now=2.0) is False
        got_a, got_b = store.get("gh:o/a:issue:4"), store.get("gh:o/b:issue:4")
        assert got_a is not None and got_a.repo == "o/a"
        assert got_b is not None and got_b.repo == "o/b"

    def test_repoless_item_round_trips_as_none(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item("7"), now=1.0)
        got = store.get("gh:issue:7")
        assert got is not None and got.repo is None

    def test_a_legacy_row_is_matched_and_backfilled(self, tmp_path: Path) -> None:
        """An upgraded daemon re-discovers items it stored without a repo:
        they must dedup, not collide on item_id."""
        store = DaemonStore(tmp_path / "state.db")
        assert store.upsert_new(item("7"), now=1.0) is True
        qualified = item("7", item_id="gh:issue:7", repo="o/r")
        assert store.upsert_new(qualified, now=2.0) is False
        got = store.get("gh:issue:7")
        assert got is not None and got.repo == "o/r"

    def test_requesters_are_scoped_per_repo(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.note_requester("4", "111", now=0.5, repo="o/a")
        store.note_requester("4", "222", now=0.5, repo="o/b")
        store.upsert_new(item("4", item_id="gh:o/a:issue:4", repo="o/a"), now=1.0)
        store.upsert_new(item("4", item_id="gh:o/b:issue:4", repo="o/b"), now=1.0)
        assert store.get("gh:o/a:issue:4").requested_by == "111"  # type: ignore[union-attr]
        assert store.get("gh:o/b:issue:4").requested_by == "222"  # type: ignore[union-attr]

    def test_a_pre_multirepo_database_gains_the_repo_columns(self, tmp_path: Path) -> None:
        """A store created before multi-repo is migrated in place, keeping
        the rows it already had."""
        path = tmp_path / "state.db"
        conn = sqlite3.connect(path)
        # The pre-multi-repo shape, verbatim: no repo column anywhere.
        conn.execute(
            "CREATE TABLE daemon_work_items (item_id TEXT PRIMARY KEY, "
            "source_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, "
            "body TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', "
            "state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
            "claimed INTEGER NOT NULL DEFAULT 0, run_id TEXT, last_error TEXT, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
            "pending_report TEXT, requested_by TEXT)"
        )
        conn.execute(
            "CREATE TABLE daemon_requesters (source_key TEXT PRIMARY KEY, "
            "requester_id TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO daemon_work_items (item_id, source_key, title, body, url, state, "
            "created_at, updated_at) VALUES ('gh:issue:7', '7', 't', 'b', '', 'queued', 1.0, 1.0)"
        )
        conn.commit()
        conn.close()

        store = DaemonStore(path)
        assert "repo" in {
            str(r[1]) for r in store._conn.execute("PRAGMA table_info(daemon_work_items)")
        }
        got = store.get("gh:issue:7")
        assert got is not None and got.repo is None

    def test_migration_drops_the_single_repo_unique_key(self, tmp_path: Path) -> None:
        """The rebuilt table keys on (source_key, repo): two repositories may
        each have an issue #4 without an IntegrityError killing the poll."""
        path = _pre_multirepo_db(tmp_path)
        store = DaemonStore(path)
        assert store.upsert_new(item("4", item_id="gh:o/a:issue:4", repo="o/a"), now=1.0) is True
        assert store.upsert_new(item("4", item_id="gh:o/b:issue:4", repo="o/b"), now=1.0) is True
        store.note_requester("4", "111", now=1.0, repo="o/a")
        store.note_requester("4", "222", now=1.0, repo="o/b")
        assert store.get("gh:o/a:issue:4").repo == "o/a"  # type: ignore[union-attr]
        assert store.get("gh:o/b:issue:4").repo == "o/b"  # type: ignore[union-attr]

    def test_a_qualified_item_never_adopts_a_repoless_row(self, tmp_path: Path) -> None:
        """Whichever repo is polled first must not claim the legacy row."""
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item("4", item_id="gh:issue:4"), now=1.0)
        assert store.upsert_new(item("4", item_id="gh:o/b:issue:4", repo="o/b"), now=2.0) is True
        legacy = store.get("gh:issue:4")
        assert legacy is not None and legacy.repo is None

    def test_backfill_names_the_sole_configured_repo(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item("4", item_id="gh:issue:4"), now=1.0)
        assert store.backfill_repo("o/a") == 1
        got = store.get("gh:issue:4")
        assert got is not None and got.repo == "o/a"
        assert store.backfill_repo("o/a") == 0
        assert store.backfill_repo(None) == 0

    def test_drop_repoless_clears_unattributable_live_rows(self, tmp_path: Path) -> None:
        """With several repos configured no name can be backfilled, so the
        non-terminal repo-less rows go and discovery re-creates them."""
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item("4", item_id="gh:issue:4"), now=1.0)
        store.upsert_new(item("5", item_id="gh:issue:5"), now=1.0)
        store.mark_done("gh:issue:5", now=2.0)
        store.upsert_new(item("6", item_id="gh:o/a:issue:6", repo="o/a"), now=1.0)

        assert store.drop_repoless() == 1
        assert store.get("gh:issue:4") is None
        # Terminal history and repo-qualified rows are untouched.
        done = store.get("gh:issue:5")
        assert done is not None and done.state == "done"
        assert store.get("gh:o/a:issue:6") is not None
        assert store.drop_repoless() == 0

    def test_drop_repoless_keeps_claimed_and_in_flight_rows(self, tmp_path: Path) -> None:
        """Claiming swaps the trigger label away, so a claimed row can never
        be rediscovered: deleting it would strand the issue silently."""
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item("4", item_id="gh:issue:4"), now=1.0)
        store.upsert_new(item("5", item_id="gh:issue:5"), now=1.0)
        store.mark_claimed("gh:issue:5", 2.0)
        store.upsert_new(item("6", item_id="gh:issue:6"), now=1.0)
        store.mark_claimed("gh:issue:6", 2.0)
        store.mark_running("gh:issue:6", "r1", 3.0)
        store.upsert_new(item("7", item_id="gh:issue:7"), now=1.0)
        store.mark_claimed("gh:issue:7", 2.0)
        store.mark_running("gh:issue:7", "r2", 3.0)
        store.mark_resume_pending("gh:issue:7", 4.0)

        # Only the untouched queued row goes.
        assert store.drop_repoless() == 1
        assert store.get("gh:issue:4") is None
        for still_here in ("gh:issue:5", "gh:issue:6", "gh:issue:7"):
            assert store.get(still_here) is not None

    def test_strand_repoless_settles_what_cannot_be_rediscovered(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item("6", item_id="gh:issue:6"), now=1.0)
        store.mark_claimed("gh:issue:6", 2.0)
        store.mark_running("gh:issue:6", "r1", 3.0)
        store.upsert_new(item("8", item_id="gh:o/a:issue:8", repo="o/a"), now=1.0)
        store.mark_done("gh:o/a:issue:8", now=2.0)

        stranded = store.strand_repoless("no repo", now=5.0)
        assert [i.item_id for i in stranded] == ["gh:issue:6"]
        assert stranded[0].url  # the operator needs the issue link
        settled = store.get("gh:issue:6")
        assert settled is not None
        assert settled.state == "failed"
        assert settled.last_error == "no repo"
        # No report debt: the repository is exactly what is unknown, so a
        # comment would land on some other repo's issue #6.
        assert settled.pending_report is None
        # The run stays pinned, so recovery reconciles against a real item.
        assert settled.run_id == "r1"
        assert store.strand_repoless("no repo", now=6.0) == []

    def test_attribute_repoless_names_rows_from_their_issue_url(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(
            item("4", item_id="gh:issue:4", url="https://github.com/o/a/issues/4"), now=1.0
        )
        store.upsert_new(
            item("5", item_id="gh:issue:5", url="https://github.com/o/z/issues/5"), now=1.0
        )
        store.upsert_new(item("6", item_id="gh:issue:6", url=""), now=1.0)

        assert store.attribute_repoless(["o/a", "o/b"]) == 1
        got = store.get("gh:issue:4")
        assert got is not None and got.repo == "o/a"
        # Unconfigured or URL-less rows are left for drop/strand to settle.
        for left in ("gh:issue:5", "gh:issue:6"):
            row = store.get(left)
            assert row is not None and row.repo is None
        assert store.attribute_repoless([]) == 0
