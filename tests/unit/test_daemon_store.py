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

    def test_blocked_row_is_terminal_for_dedup(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_blocked("gh:7", "405", now=2.0)
        assert store.upsert_new(item(), now=3.0) is False
        assert store.upsert_new(item(body="edited"), now=4.0) is True

    def test_running_row_never_superseded(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:7", "r1", now=2.0)
        assert store.upsert_new(item(body="changed"), now=3.0) is False

    def test_requester_recorded_by_the_concierge_lands_on_the_item(self, tmp_path: Path) -> None:
        """The concierge files the issue and knows who asked; discovery
        builds the item later from GitHub, which does not — the store
        joins the two, and the id never appears in the public issue."""
        store = DaemonStore(tmp_path / "state.db")
        store.note_requester("7", "1234567890", now=0.5)
        store.upsert_new(item(), now=1.0)
        got = store.get("gh:7")
        assert got is not None and got.requested_by == "1234567890"
        # An item the source already attributed keeps its own attribution.
        store.note_requester("8", "999", now=0.5)
        store.upsert_new(item("8", requested_by="111"), now=1.0)
        assert store.get("gh:8").requested_by == "111"  # type: ignore[union-attr]
        assert store.get("gh:7").requested_by == "1234567890"  # type: ignore[union-attr]
        # Nothing recorded → nothing attributed.
        store.upsert_new(item("9"), now=1.0)
        assert store.get("gh:9").requested_by is None  # type: ignore[union-attr]


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

    def test_attempt_counting_requeue_vs_failed(self, tmp_path: Path) -> None:
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
        assert got.state == "failed" and got.attempts == 2 and got.last_error == "err2"

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

    def test_blocked_is_terminal_until_a_human_retries(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:7", "r1", now=2.0)
        store.mark_blocked("gh:7", "GitHub refused the merge", now=3.0)
        got = store.get("gh:7")
        assert got is not None and got.state == "blocked" and got.run_id == "r1"
        assert got.last_error == "GitHub refused the merge"
        assert got.pending_report == "blocked"  # the source is owed the label
        assert store.next_queued(now=1e9, backoff_s=1) is None
        assert [i.item_id for i in store.items(["blocked"])] == ["gh:7"]
        with pytest.raises(ValueError, match="use retry"):
            store.requeue("gh:7", now=4.0)
        got = store.retry("gh:7", now=5.0)
        assert got.state == "queued" and got.attempts == 0 and got.run_id is None
        assert got.pending_report == "requeued"

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
        store.mark_running("gh:5", "r5", now=2.0)
        assert store.item_for_run("r5") == "gh:5"
        assert store.item_for_run("r-unknown") is None
        assert store.runs_for_item("gh:5") == ["r5"]

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
        store.mark_running("gh:7", "r1", now=2.0)
        got = store.abandon("gh:7", "spiraling plan", now=3.0)
        assert got.state == "failed" and got.run_id == "r1"
        assert got.last_error == "spiraling plan"
        with pytest.raises(ValueError, match="already failed"):
            store.abandon("gh:7", "again", now=4.0)
        with pytest.raises(KeyError):
            store.abandon("gh:404", "x", now=4.0)

    def test_abandon_of_a_blocked_item_is_allowed(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_blocked("gh:7", "405", now=2.0)
        got = store.abandon("gh:7", "not worth it", now=3.0)
        assert got.state == "failed" and got.pending_report == "abandoned"

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
        assert [i.item_id for i in store.items(["failed"])] == ["gh:2"]
        assert [i.item_id for i in store.items(["queued", "failed"])] == ["gh:1", "gh:2"]
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

    def test_a_merged_run_owes_the_close_until_it_lands(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:7", "r1", now=2.0)
        store.mark_done("gh:7", now=3.0, pending_report="merged")
        got = store.get("gh:7")
        assert got is not None and got.state == "done" and got.pending_report == "merged"
        assert [i.item_id for i in store.pending_reports()] == ["gh:7"]
        assert store.take_pending_report("gh:7") is True
        assert store.get("gh:7").pending_report is None  # type: ignore[union-attr]
        # plain mark_done owes nothing
        store.upsert_new(item("8"), now=1.0)
        store.mark_done("gh:8", now=2.0)
        assert store.get("gh:8").pending_report is None  # type: ignore[union-attr]


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
                INSERT INTO daemon_work_items VALUES ('gh:1','github','1','patch','x','done');
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
        assert store.get("gh:1") is None
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
