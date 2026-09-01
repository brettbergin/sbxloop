"""Upgrade path for daemon state written before restart-by-label (#600).

Restarting an issue by re-adding the trigger label changed what a persisted
``daemon_work_items`` row *means*: a terminal row is now re-queueable, and
three new columns (``prior_run_id`` / ``prior_branch`` / ``prior_pr_number``)
carry what the finished attempt pushed to origin. A deployed daemon has none
of that on disk, so every database here is hand-written raw SQL in the
pre-change shape (``tests.fakes.legacy_db``) — never produced by the new
code, which would normalise away exactly what is under test — opened with
the new :class:`DaemonStore` and then exercised.

The sweep is by row shape, because an upgrade lands on all of them at once:
queued, claimed-but-unstarted, resume-pending, running, and each terminal
state (done / failed / blocked / cancelled, plus a row an operator abandoned,
which is persisted as ``failed``). Both id spellings a deployed store can
hold — legacy bare ``gh:<n>`` and typed ``gh:issue:<n>`` — are covered.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import DaemonStore
from tests.fakes.legacy_db import daemon_db, insert_daemon_row, raw_daemon_rows

REPO = "o/r"
NEW_COLUMNS = ("prior_run_id", "prior_branch", "prior_pr_number")


def old_db(tmp_path: Path) -> Path:
    """A raw daemon state.db in the shape released before #600."""
    return daemon_db(tmp_path, "pre_prior_attempt")


def old_row(db: Path, key: str, *, item_id: str | None = None, **fields: Any) -> str:
    """One hand-written pre-change row; returns the id as stored."""
    stored = item_id or f"gh:issue:{key}"
    insert_daemon_row(
        db,
        item_id=stored,
        source_key=key,
        title=f"Item {key}",
        body="do the thing",
        url=f"https://github.com/{REPO}/issues/{key}",
        repo=REPO,
        **fields,
    )
    return stored


def rediscovered(key: str, *, title: str | None = None, body: str = "do the thing") -> WorkItem:
    """What a poll that saw the trigger label hands ``upsert_new``: the same
    issue, unchanged text, typed id."""
    return WorkItem(
        item_id=f"gh:issue:{key}",
        source_key=key,
        title=title or f"Item {key}",
        body=body,
        url=f"https://github.com/{REPO}/issues/{key}",
        repo=REPO,
    )


def columns(db: Path, table: str = "daemon_work_items") -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}  # nosec B608
    finally:
        conn.close()


class TestSchemaMigration:
    def test_missing_columns_are_added_and_default_empty(self, tmp_path: Path) -> None:
        db = old_db(tmp_path)
        old_row(db, "1", state="queued", claimed=0, run_id=None)
        assert not (columns(db) & set(NEW_COLUMNS)), "fixture must predate the columns"

        store = DaemonStore(db)
        assert set(NEW_COLUMNS) <= columns(db)
        got = store.get("gh:issue:1")
        assert got is not None
        assert (got.prior_run_id, got.prior_branch, got.prior_pr_number) == (None, None, None)
        assert got.restarted is False
        store.close()

    def test_opening_twice_is_idempotent_and_keeps_every_row(self, tmp_path: Path) -> None:
        db = old_db(tmp_path)
        for n, state in enumerate(("queued", "running", "done", "cancelled"), start=1):
            old_row(db, str(n), state=state, claimed=1, run_id=f"r{n}")
        before = raw_daemon_rows(db)
        DaemonStore(db).close()
        DaemonStore(db).close()
        assert raw_daemon_rows(db) == before


class TestLiveRowsAreUntouched:
    """A poll of the same unchanged issue must not disturb work in flight."""

    def test_queued_unclaimed_row_stays_queued_and_dispatchable(self, tmp_path: Path) -> None:
        db = old_db(tmp_path)
        old_row(db, "1", state="queued", claimed=0, run_id=None)
        store = DaemonStore(db)

        assert store.upsert_new(rediscovered("1"), now=9.0) is False, "already queued"
        got = store.get("gh:issue:1")
        assert got is not None and got.state == "queued" and got.run_id is None
        nxt = store.next_queued(now=10.0, backoff_s=60.0)
        assert nxt is not None and nxt.item_id == "gh:issue:1"
        store.close()

    def test_claimed_row_keeps_its_claim_and_is_not_requeued(self, tmp_path: Path) -> None:
        db = old_db(tmp_path)
        old_row(db, "2", state="queued", claimed=1, run_id=None)
        store = DaemonStore(db)

        assert store.upsert_new(rediscovered("2"), now=9.0) is False
        got = store.get("gh:issue:2")
        assert got is not None and got.claimed is True and got.state == "queued"
        store.close()

    def test_running_row_keeps_its_pinned_run_and_settles(self, tmp_path: Path) -> None:
        db = old_db(tmp_path)
        old_row(db, "3", state="running", claimed=1, run_id="r_live", attempts=1)
        store = DaemonStore(db)

        assert store.upsert_new(rediscovered("3"), now=9.0) is False
        got = store.get("gh:issue:3")
        assert got is not None and got.state == "running" and got.run_id == "r_live"
        assert [i.item_id for i in store.running_items()] == ["gh:issue:3"]

        store.mark_done("gh:issue:3", now=11.0, pending_report="merged")
        settled = store.get("gh:issue:3")
        assert settled is not None and settled.state == "done"
        store.close()

    def test_resume_pending_row_is_invisible_to_discovery_and_resumes(self, tmp_path: Path) -> None:
        db = old_db(tmp_path)
        old_row(db, "4", state="queued", claimed=1, run_id="r_resume", attempts=1)
        store = DaemonStore(db)

        assert store.upsert_new(rediscovered("4"), now=9.0) is False
        pending = store.next_queued(now=10.0, backoff_s=600.0)
        assert pending is not None and pending.run_id == "r_resume", "resumes skip the backoff"

        store.mark_resuming("gh:issue:4", "r_resume", now=11.0)
        resumed = store.get("gh:issue:4")
        assert resumed is not None
        assert (resumed.state, resumed.run_id, resumed.attempts) == ("running", "r_resume", 1)
        store.close()

    def test_gated_row_is_not_requeued_by_a_poll(self, tmp_path: Path) -> None:
        db = old_db(tmp_path)
        old_row(db, "5", state="gated", claimed=1, run_id="r_gate")
        store = DaemonStore(db)

        assert store.upsert_new(rediscovered("5"), now=9.0) is False
        got = store.get("gh:issue:5")
        assert got is not None and got.state == "gated" and got.run_id == "r_gate"
        store.close()


TERMINAL_ROWS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("done", {"claimed": 1, "run_id": "r_done", "pending_report": "merged"}),
    ("failed", {"claimed": 1, "run_id": "r_failed", "last_error": "gave up"}),
    ("blocked", {"claimed": 1, "run_id": "r_blocked", "pending_report": "blocked"}),
    ("cancelled", {"claimed": 1, "run_id": "r_cancelled"}),
    # An operator-abandoned item is persisted as ``failed`` with the reason.
    ("failed", {"claimed": 1, "run_id": "r_aband", "last_error": "abandoned by operator"}),
)


class TestTerminalRowsRestartByLabel:
    @pytest.mark.parametrize(
        ("state", "fields"), TERMINAL_ROWS, ids=[f"{s}-{f['run_id']}" for s, f in TERMINAL_ROWS]
    )
    def test_terminal_row_with_unchanged_text_is_requeued(
        self, tmp_path: Path, state: str, fields: dict[str, Any]
    ) -> None:
        db = old_db(tmp_path)
        old_row(db, "7", state=state, attempts=3, **fields)
        store = DaemonStore(db)

        assert store.upsert_new(rediscovered("7"), now=9.0) is True, "the label is never inert"
        got = store.get("gh:issue:7")
        assert got is not None
        assert (got.state, got.claimed, got.run_id, got.attempts, got.last_error) == (
            "queued",
            False,
            None,
            0,
            None,
        )
        assert got.pending_report is None
        # The finished attempt's run is remembered so the restart continues it.
        prior = store.prior_attempt("gh:issue:7")
        assert prior is not None and prior.run_id == fields["run_id"]
        assert store.next_queued(now=10.0, backoff_s=60.0) is not None
        store.close()

    def test_the_requeue_is_logged_with_its_reason(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        db = old_db(tmp_path)
        old_row(db, "8", state="cancelled", claimed=1, run_id="r_old")
        store = DaemonStore(db)
        with caplog.at_level(logging.INFO, logger="sbxloop.daemon.store"):
            assert store.upsert_new(rediscovered("8"), now=9.0) is True
        messages = [r.getMessage() for r in caplog.records]
        assert any("store.item_requeued_by_label" in m for m in messages)
        store.close()

    def test_a_terminal_row_whose_text_changed_is_superseded(self, tmp_path: Path) -> None:
        """Editing the issue as well as re-labelling it is still a new item —
        the restart path must not swallow the supersede path."""
        db = old_db(tmp_path)
        old_row(db, "9", state="done", claimed=1, run_id="r_done")
        store = DaemonStore(db)

        assert store.upsert_new(rediscovered("9", body="edited"), now=9.0) is True
        got = store.get("gh:issue:9")
        assert got is not None and got.state == "queued" and got.body == "edited"
        store.close()


class TestLegacyIdSpellings:
    def test_bare_and_qualified_ids_resolve_to_one_row(self, tmp_path: Path) -> None:
        db = old_db(tmp_path)
        old_row(db, "11", item_id="gh:11", state="cancelled", claimed=1, run_id="r_old")
        store = DaemonStore(db)

        for spelling in ("gh:11", "gh:issue:11"):
            got = store.get(spelling)
            assert got is not None and got.item_id == "gh:issue:11"
        assert len(store.items()) == 1
        store.close()

    def test_an_old_bare_id_row_restarts_by_label(self, tmp_path: Path) -> None:
        db = old_db(tmp_path)
        old_row(db, "12", item_id="gh:12", state="failed", claimed=1, run_id="r_old", attempts=2)
        store = DaemonStore(db)

        assert store.upsert_new(rediscovered("12"), now=9.0) is True
        for spelling in ("gh:12", "gh:issue:12"):
            got = store.get(spelling)
            assert got is not None and got.state == "queued" and got.attempts == 0
            prior = store.prior_attempt(spelling)
            assert prior is not None and prior.run_id == "r_old"
        # Re-queued in place: still one row, still under its stored key.
        assert [row for row, _ in raw_daemon_rows(db)] == ["gh:12"]
        store.close()


class TestPriorArtifactRecovery:
    def _engine_run(self, db: Path, run_id: str, **fields: Any) -> None:
        """A ``runs`` row as the engine wrote it in the shared state.db."""
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, outcome TEXT NOT NULL, "
            "state TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}', "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL, branch TEXT, pr_number INTEGER)"
        )
        row: dict[str, Any] = {
            "run_id": run_id,
            "outcome": "cancelled",
            "state": "cancelled",
            "created_at": 1.0,
            "updated_at": 1.0,
        }
        row.update(fields)
        names = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        conn.execute(
            f"INSERT INTO runs ({names}) VALUES ({marks})",  # nosec B608 - test SQL
            tuple(row.values()),
        )
        conn.commit()
        conn.close()

    def test_branch_and_pr_are_recovered_from_the_old_run_record(self, tmp_path: Path) -> None:
        db = old_db(tmp_path)
        old_row(db, "21", state="cancelled", claimed=1, run_id="r_old")
        self._engine_run(db, "r_old", branch="sbxloop/r_old", pr_number=42)
        store = DaemonStore(db)

        assert store.upsert_new(rediscovered("21"), now=9.0) is True
        prior = store.prior_attempt("gh:issue:21")
        assert prior is not None
        assert (prior.run_id, prior.branch, prior.pr_number) == ("r_old", "sbxloop/r_old", 42)
        store.close()

    def test_a_run_that_never_pushed_leaves_no_branch_to_reuse(self, tmp_path: Path) -> None:
        """The engine row exists but has no branch: the restart carries the
        run id only, so the engine starts fresh rather than erroring."""
        db = old_db(tmp_path)
        old_row(db, "22", state="failed", claimed=1, run_id="r_nobranch")
        self._engine_run(db, "r_nobranch", branch=None, pr_number=None)
        store = DaemonStore(db)

        assert store.upsert_new(rediscovered("22"), now=9.0) is True
        prior = store.prior_attempt("gh:issue:22")
        assert prior is not None and prior.branch is None and prior.pr_number is None
        store.close()

    def test_a_terminal_row_with_no_run_at_all_restarts_clean(self, tmp_path: Path) -> None:
        """Nothing was ever dispatched (an item failed before its first run):
        no prior attempt, no error — just a queued row."""
        db = old_db(tmp_path)
        old_row(db, "23", state="failed", claimed=0, run_id=None, last_error="no runner")
        store = DaemonStore(db)

        assert store.upsert_new(rediscovered("23"), now=9.0) is True
        assert store.prior_attempt("gh:issue:23") is None
        got = store.get("gh:issue:23")
        assert got is not None and got.state == "queued" and got.last_error is None
        store.close()

    def test_the_ledger_supplies_the_run_when_the_row_lost_it(self, tmp_path: Path) -> None:
        """An old row cleared of its run_id still has the daemon ledger, so
        the last run it was dispatched under is what the restart continues."""
        db = old_db(tmp_path)
        old_row(db, "24", item_id="gh:24", state="cancelled", claimed=1, run_id=None)
        store = DaemonStore(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO daemon_runs (run_id, item_id, started_at, finished_at, result) "
            "VALUES ('r_ledger', 'gh:24', 1.0, 2.0, 'cancelled')"
        )
        conn.commit()
        conn.close()

        assert store.upsert_new(rediscovered("24"), now=9.0) is True
        prior = store.prior_attempt("gh:issue:24")
        assert prior is not None and prior.run_id == "r_ledger"
        store.close()

    def test_a_missing_runs_table_is_not_an_error(self, tmp_path: Path) -> None:
        """A daemon state.db that never held an engine history at all."""
        db = old_db(tmp_path)
        old_row(db, "25", state="blocked", claimed=1, run_id="r_gone")
        store = DaemonStore(db)

        assert store.upsert_new(rediscovered("25"), now=9.0) is True
        prior = store.prior_attempt("gh:issue:25")
        assert prior is not None and prior.run_id == "r_gone" and prior.branch is None
        store.close()

    def test_a_pre_1_0_runs_shape_without_pr_columns_is_tolerated(self, tmp_path: Path) -> None:
        db = old_db(tmp_path)
        old_row(db, "26", state="cancelled", claimed=1, run_id="r_old")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, outcome TEXT NOT NULL, "
            "state TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}', "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO runs (run_id, outcome, state, created_at, updated_at) "
            "VALUES ('r_old', 'cancelled', 'cancelled', 1.0, 1.0)"
        )
        conn.commit()
        conn.close()
        store = DaemonStore(db)

        assert store.upsert_new(rediscovered("26"), now=9.0) is True
        prior = store.prior_attempt("gh:issue:26")
        assert prior is not None and prior.run_id == "r_old" and prior.branch is None
        store.close()


class TestWholeDatabaseSweep:
    def test_every_shape_a_deployed_store_can_hold_upgrades_together(self, tmp_path: Path) -> None:
        """One database holding every (id form x state) combination opens,
        keeps every row, and answers the restart question per row: live rows
        are left alone, terminal rows come back to the queue."""
        db = old_db(tmp_path)
        shapes: tuple[dict[str, Any], ...] = (
            {"state": "queued", "claimed": 0, "run_id": None},
            {"state": "queued", "claimed": 1, "run_id": None},
            {"state": "queued", "claimed": 1, "run_id": "r_resume"},
            {"state": "running", "claimed": 1, "run_id": "r_live"},
            {"state": "gated", "claimed": 1, "run_id": "r_gate"},
            {"state": "done", "claimed": 1, "run_id": "r_done"},
            {"state": "failed", "claimed": 1, "run_id": "r_failed"},
            {"state": "blocked", "claimed": 1, "run_id": "r_blocked"},
            {"state": "cancelled", "claimed": 1, "run_id": "r_cancelled"},
        )
        keys: list[tuple[str, str]] = []
        n = 100
        for form in ("gh:{n}", "gh:issue:{n}"):
            for shape in shapes:
                key = str(n)
                old_row(db, key, item_id=form.format(n=n), **shape)
                keys.append((key, str(shape["state"])))
                n += 1

        store = DaemonStore(db)
        assert len(store.items()) == len(keys)
        for key, state in keys:
            requeued = store.upsert_new(rediscovered(key), now=500.0)
            terminal = state in ("done", "failed", "blocked", "cancelled")
            assert requeued is terminal, f"{key} ({state}) took the wrong path"
            got = store.get(f"gh:issue:{key}")
            assert got is not None
            assert got.state == ("queued" if terminal else state)
        assert len(store.items()) == len(keys), "restarts happen in place, never as new rows"
        store.close()
