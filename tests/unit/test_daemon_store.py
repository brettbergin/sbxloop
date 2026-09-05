"""DaemonStore: work-item bookkeeping the outer loop depends on."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import LEGACY_SUFFIX, SCHEMA_VERSION, DaemonStore
from sbxloop.engine.store import StateStore
from sbxloop.errors import DaemonError
from tests.fakes.legacy_db import daemon_db, insert_daemon_row


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
    return daemon_db(tmp_path, "pre_multirepo")


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
        # identical re-discovery of a done item re-queues it (#600): the
        # issue is only polled while it carries the trigger label.
        assert store.upsert_new(item(), now=3.0) is True
        store.mark_done("gh:issue:7", now=3.5)
        # edited content is
        assert store.upsert_new(item(body="do MORE things"), now=4.0) is True
        got = store.get("gh:issue:7")
        assert got is not None and got.state == "queued" and got.body == "do MORE things"

    def test_blocked_row_is_terminal_for_dedup(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_blocked("gh:issue:7", "405", now=2.0)
        assert store.upsert_new(item(), now=3.0) is True  # re-queued (#600)
        store.mark_blocked("gh:issue:7", "405", now=3.5)
        assert store.upsert_new(item(body="edited"), now=4.0) is True

    def test_unchanged_terminal_row_is_requeued_by_the_label(self, tmp_path: Path) -> None:
        """#600: re-adding `sbxloop:run` to an unchanged issue whose last
        attempt is finished restarts it — the label is never inert."""
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        store.mark_failed("gh:issue:7", "boom", now=3.0, requeue=False)
        assert store.upsert_new(item(), now=4.0) is True
        got = store.get("gh:issue:7")
        assert got is not None
        assert (got.state, got.claimed, got.run_id, got.last_error) == ("queued", False, None, None)
        assert got.attempts == 0 and got.updated_at == 4.0

    @pytest.mark.parametrize("terminal", ["done", "failed", "blocked", "cancelled"])
    def test_every_terminal_state_is_requeued_unchanged(
        self, tmp_path: Path, terminal: str
    ) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.set_state("gh:issue:7", terminal, now=2.0)  # type: ignore[arg-type]
        assert store.upsert_new(item(), now=3.0) is True
        got = store.get("gh:issue:7")
        assert got is not None and got.state == "queued"

    @pytest.mark.parametrize(
        "prepare",
        [
            pytest.param(lambda st: None, id="queued"),
            pytest.param(lambda st: st.mark_claimed("gh:issue:7", 2.0), id="claimed"),
            pytest.param(lambda st: st.mark_running("gh:issue:7", "r1", 2.0), id="running"),
            pytest.param(
                lambda st: (
                    st.mark_running("gh:issue:7", "r1", 2.0),
                    st.mark_resume_pending("gh:issue:7", 2.5),
                ),
                id="resume-pending",
            ),
        ],
    )
    def test_live_rows_still_dedup(self, tmp_path: Path, prepare: object) -> None:
        """A live item is never reset by a poll — that would double-dispatch."""
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        prepare(store)  # type: ignore[operator]
        before = store.get("gh:issue:7")
        assert store.upsert_new(item(), now=9.0) is False
        assert store.get("gh:issue:7") == before

    def test_requeue_by_label_keeps_the_prior_branch_and_pr(self, tmp_path: Path) -> None:
        """The restart must continue the branch the cancelled attempt
        pushed, not redo it (#600)."""
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        store.record_prior_attempt("gh:issue:7", run_id="r1", branch="sbxloop/gh-7", pr_number=42)
        store.mark_cancelled("gh:issue:7", "cancelled by b", now=3.0)
        assert store.upsert_new(item(), now=4.0) is True
        prior = store.prior_attempt("gh:issue:7")
        assert prior is not None
        assert (prior.run_id, prior.branch, prior.pr_number) == ("r1", "sbxloop/gh-7", 42)

    def test_discarding_the_requeued_row_keeps_the_prior_branch_and_pr(
        self, tmp_path: Path
    ) -> None:
        """The loop discards a queued row on every unsuccessful claim (another
        daemon won, GitHub was down) and relies on the next poll to re-create
        it. That re-created row must still offer the previous attempt's
        branch and PR — otherwise the restart silently starts from scratch
        and abandons the pushed work (#600)."""
        store = DaemonStore(tmp_path / "state.db")
        repo_item = item(repo="o/r", item_id="gh:issue:7")
        store.upsert_new(repo_item, now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        store.record_prior_attempt("gh:issue:7", run_id="r1", branch="sbxloop/r1", pr_number=9)
        store.mark_cancelled("gh:issue:7", "cancelled", now=3.0)
        assert store.upsert_new(repo_item, now=4.0) is True

        assert store.discard("gh:issue:7") is True  # the lost-claim path
        assert store.upsert_new(repo_item, now=5.0) is True
        prior = store.prior_attempt("gh:issue:7")
        assert prior is not None
        assert (prior.run_id, prior.branch, prior.pr_number) == ("r1", "sbxloop/r1", 9)

    def test_dropping_a_repoless_row_keeps_the_prior_branch_and_pr(self, tmp_path: Path) -> None:
        """``drop_repoless`` deletes rows for discovery to re-create
        repo-qualified; the prior attempt must survive that too (#600)."""
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)  # repo-less, as a legacy row is
        store.record_prior_attempt("gh:issue:7", run_id="r1", branch="sbxloop/r1", pr_number=9)
        assert store.drop_repoless() == 1
        assert store.get("gh:issue:7") is None

        assert store.upsert_new(item(repo="o/r", item_id="gh:o/r:issue:7"), now=2.0) is True
        prior = store.prior_attempt("gh:o/r:issue:7")
        assert prior is not None
        assert (prior.run_id, prior.branch, prior.pr_number) == ("r1", "sbxloop/r1", 9)

    def test_an_edited_issue_still_continues_the_prior_branch(self, tmp_path: Path) -> None:
        """Superseding a terminal row deletes it and INSERTs a fresh one. The
        work the last attempt pushed is still on origin, so the new run
        continues it rather than opening a second branch (#600)."""
        store = DaemonStore(tmp_path / "state.db")
        repo_item = item(repo="o/r", item_id="gh:issue:7")
        store.upsert_new(repo_item, now=1.0)
        store.record_prior_attempt("gh:issue:7", run_id="r1", branch="sbxloop/r1", pr_number=9)
        store.mark_failed("gh:issue:7", "boom", now=2.0, requeue=False)
        assert store.upsert_new(item(repo="o/r", item_id="gh:issue:7", body="edited"), 3.0) is True
        prior = store.prior_attempt("gh:issue:7")
        assert prior is not None and prior.branch == "sbxloop/r1"

    def test_a_re_created_row_for_an_untouched_issue_offers_nothing(self, tmp_path: Path) -> None:
        """No previous attempt means no offer: a first-ever claim must not
        invent one from another issue's history."""
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(repo="o/r", item_id="gh:issue:7"), now=1.0)
        store.record_prior_attempt("gh:issue:7", run_id="r1", branch="sbxloop/r1", pr_number=9)
        assert store.upsert_new(item("8", repo="o/r", item_id="gh:issue:8"), now=2.0) is True
        assert store.prior_attempt("gh:issue:8") is None

    def test_prior_attempt_is_per_repository(self, tmp_path: Path) -> None:
        """The same issue number in two repositories is two work items with
        two branches; one must never be offered for the other."""
        store = DaemonStore(tmp_path / "state.db")
        a = item(repo="o/a", item_id="gh:o/a:issue:7")
        b = item(repo="o/b", item_id="gh:o/b:issue:7")
        store.upsert_new(a, now=1.0)
        store.upsert_new(b, now=1.0)
        store.record_prior_attempt("gh:o/a:issue:7", run_id="ra", branch="sbxloop/ra")
        store.discard("gh:o/b:issue:7")
        assert store.upsert_new(b, now=2.0) is True
        assert store.prior_attempt("gh:o/b:issue:7") is None
        assert store.discard("gh:o/a:issue:7") is True
        assert store.upsert_new(a, now=3.0) is True
        prior = store.prior_attempt("gh:o/a:issue:7")
        assert prior is not None and prior.branch == "sbxloop/ra"

    def test_prior_attempt_is_none_without_pushed_work(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        assert store.prior_attempt("gh:issue:7") is None
        assert store.prior_attempt("gh:issue:404") is None

    def test_changed_terminal_row_is_still_superseded(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_cancelled("gh:issue:7", "by hand", now=2.0)
        assert store.upsert_new(item(body="edited"), now=3.0) is True
        got = store.get("gh:issue:7")
        assert got is not None and got.state == "queued" and got.body == "edited"

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
        # Re-discovery restarts it (#600); put it back so the operator
        # paths below are exercised from the cancelled state.
        assert store.upsert_new(item(), now=4.0) is True
        store.mark_running("gh:issue:7", "r1", now=4.2)
        store.mark_cancelled("gh:issue:7", "cancelled by op", now=4.5)
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
        # ...and the generic text view of the same row
        assert store.chat_thread("r1") == ("11", "22", "33", "44", "discord")
        assert store.run_for_thread("22") == "r1" and store.run_for_thread(22) == "r1"
        assert store.run_for_thread(99) is None

    def test_chat_thread_keeps_slack_timestamps_exact(self, tmp_path: Path) -> None:
        """Slack ids are message timestamps; INTEGER affinity would have
        rounded "1724968573.123456" into a REAL and broken every lookup."""
        store = DaemonStore(tmp_path / "state.db")
        ts = "1724968573.123456"
        store.record_chat_thread("r1", "C0123ABCDEF", ts, ts, backend="slack")
        store.set_chat_status_id("r1", "1724968574.000100")
        assert store.chat_thread("r1") == ("C0123ABCDEF", ts, ts, "1724968574.000100", "slack")
        assert store.run_for_thread(ts) == "r1"
        assert store.run_for_thread("1724968573.123457") is None

    def test_one_run_has_a_thread_per_backend(self, tmp_path: Path) -> None:
        """The daemon runs the operator console's local bridge beside the
        external one, so a run has one thread row per backend; the bare
        lookup prefers the external row (what the concierge links), the
        keyed lookup is exact, and thread ids resolve within their backend."""
        store = DaemonStore(tmp_path / "state.db")
        store.record_chat_thread("r1", "control", "thread:5", "5", backend="local")
        store.record_chat_thread("r1", "42", "4242", "100", backend="discord")
        assert store.chat_thread("r1", "local") == ("control", "thread:5", "5", None, "local")
        assert store.chat_thread("r1", "discord") == ("42", "4242", "100", None, "discord")
        assert store.chat_thread("r1") == ("42", "4242", "100", None, "discord")
        assert store.chat_thread("r1", "slack") is None
        store.set_chat_status_id("r1", "6", backend="local")
        store.set_chat_status_id("r1", "101", backend="discord")
        assert store.chat_thread("r1", "local").status_id == "6"  # type: ignore[union-attr]
        assert store.chat_thread("r1", "discord").status_id == "101"  # type: ignore[union-attr]
        assert store.run_for_thread("thread:5", "local") == "r1"
        assert store.run_for_thread("thread:5", "discord") is None
        assert store.run_for_thread("4242") == "r1"

    def test_the_bare_lookup_never_answers_with_the_local_thread(self, tmp_path: Path) -> None:
        """An external bridge cannot spell a pointer to the console's
        thread, so prose that asks bare gets none rather than a dead link."""
        store = DaemonStore(tmp_path / "state.db")
        store.record_chat_thread("r1", "control", "thread:5", "5", backend="local")
        assert store.chat_thread("r1") is None
        assert store.chat_thread("r1", "local") is not None

    def test_single_keyed_thread_table_is_rekeyed_per_backend(self, tmp_path: Path) -> None:
        """A store written before the local bridge keys threads by run
        alone; opening it rebuilds the table on (run, backend) with the
        rows intact, once."""
        path = tmp_path / "state.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE daemon_chat_threads (
                run_id TEXT PRIMARY KEY, backend TEXT NOT NULL DEFAULT 'discord',
                channel_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                headline_id TEXT, status_id TEXT);
            INSERT INTO daemon_chat_threads VALUES ('r1', 'discord', '42', '4242', '100', NULL);
            INSERT INTO daemon_chat_threads VALUES ('r2', 'slack', 'C1', '17.5', '17.5', '18.0');
            """
        )
        conn.commit()
        conn.close()
        store = DaemonStore(path)
        assert store.chat_thread("r1") == ("42", "4242", "100", None, "discord")
        assert store.chat_thread("r2", "slack") == ("C1", "17.5", "17.5", "18.0", "slack")
        store.record_chat_thread("r1", "control", "thread:9", "9", backend="local")
        assert store.chat_thread("r1", "discord") is not None
        assert store.chat_thread("r1", "local") is not None
        pk = [
            r[1] for r in store._conn.execute("PRAGMA table_info(daemon_chat_threads)") if r[5] > 0
        ]
        assert pk == ["run_id", "backend"]
        store.close()
        assert DaemonStore(path).chat_thread("r2", "slack") is not None

    def test_pre_slack_discord_threads_table_is_migrated(self, tmp_path: Path) -> None:
        """A store written before [chat] existed carries the rows in
        ``daemon_discord_threads`` with INTEGER ids; opening it folds them
        into ``daemon_chat_threads`` once and drops the old table."""
        import sqlite3

        path = tmp_path / "state.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE daemon_discord_threads (
                run_id TEXT PRIMARY KEY, channel_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL, headline_id INTEGER, status_id INTEGER);
            INSERT INTO daemon_discord_threads VALUES ('r1', 42, 4242, 100, NULL);
            INSERT INTO daemon_discord_threads VALUES ('r2', 42, 4343, 101, 102);
            """
        )
        conn.commit()
        conn.close()
        store = DaemonStore(path)
        assert store.discord_thread("r1") == (42, 4242, 100, None)
        assert store.chat_thread("r2") == ("42", "4343", "101", "102", "discord")
        assert store.run_for_thread(4343) == "r2"
        tables = {
            r[0] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "daemon_discord_threads" not in tables
        # Reopening is a no-op.
        assert DaemonStore(path).chat_thread("r1") is not None

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

    def test_strand_repoless_settles_a_row_stored_under_the_bare_legacy_id(
        self, tmp_path: Path
    ) -> None:
        """The upgrade path's likely shape: a store written before typed ids
        (#508) holds ``gh:<n>`` keys. ``_row_to_item`` normalises the id, so
        the settle must bind the id as stored — otherwise the UPDATE matches
        nothing, the row is reported settled while staying ``running``, and
        every daemon start strands it again."""
        path = _pre_multirepo_db(tmp_path)
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO daemon_work_items (item_id, source_key, title, url, state, "
            "claimed, run_id, created_at, updated_at, pending_report) "
            "VALUES ('gh:7', '7', 'old', 'https://x/issues/7', 'running', 1, 'r1', 1.0, 1.0, "
            "'merged')"
        )
        conn.commit()
        conn.close()
        store = DaemonStore(path)

        stranded = store.strand_repoless("no repo", now=5.0)

        assert [i.item_id for i in stranded] == ["gh:issue:7"]
        row = store.get("gh:7")
        assert row is not None
        assert row.state == "failed"
        assert row.last_error == "no repo"
        assert row.pending_report is None
        assert row.run_id == "r1"
        # Idempotent: the row really was settled, so nothing is left to strand.
        assert store.strand_repoless("no repo", now=6.0) == []
        raw = (
            sqlite3.connect(path).execute("SELECT item_id, state FROM daemon_work_items").fetchall()
        )
        assert raw == [("gh:7", "failed")]

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


def _pre_not_before_db(tmp_path: Path) -> Path:
    """A state.db in the multi-repo shape from before scheduled retries
    (#523): `daemon_work_items` has `repo` but no `not_before`."""
    path = daemon_db(tmp_path, "pre_scheduled_retry")
    insert_daemon_row(
        path,
        item_id="gh:o/r:issue:3",
        source_key="3",
        title="Three",
        state="queued",
        attempts=1,
        claimed=1,
        run_id="r_old",
        created_at=1.0,
        updated_at=2.0,
        repo="o/r",
    )
    return path


class TestScheduledRetries:
    """`not_before` (#523): an exhausted run's item waits its own clock,
    pinned to the run it will resume."""

    def test_not_before_gates_dispatch_even_for_a_pinned_run(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_claimed("gh:issue:7", now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        store.mark_exhausted("gh:issue:7", "one round short", now=3.0, not_before=103.0)
        got = store.get("gh:issue:7")
        assert got is not None and got.state == "queued" and got.run_id == "r1"
        assert got.not_before == 103.0 and got.attempts == 1
        assert got.last_error == "one round short"
        # Pinned runs skip the attempt backoff, but not their own clock.
        assert store.next_queued(now=50.0, backoff_s=0.0) is None
        assert store.next_queued(now=103.0, backoff_s=0.0) is not None
        # Resuming clears the clock.
        store.mark_resuming("gh:issue:7", "r1", now=103.0)
        got = store.get("gh:issue:7")
        assert got is not None and got.state == "running" and got.not_before is None

    def test_a_scheduled_item_does_not_block_the_queue(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_claimed("gh:issue:7", now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        store.mark_exhausted("gh:issue:7", "x", now=3.0, not_before=500.0)
        store.upsert_new(item(source_key="8", item_id="gh:issue:8"), now=4.0)
        nxt = store.next_queued(now=10.0, backoff_s=0.0)
        assert nxt is not None and nxt.item_id == "gh:issue:8"

    def test_resume_exhausted_from_failed_owes_a_requeued_report(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_claimed("gh:issue:7", now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        store.mark_failed("gh:issue:7", "exhausted again", now=3.0, requeue=False)
        fresh = store.resume_exhausted("gh:issue:7", "r1", 4.0, "2 more granted by brett")
        assert fresh.state == "queued" and fresh.run_id == "r1" and fresh.not_before is None
        assert fresh.pending_report == "requeued" and fresh.last_error == "2 more granted by brett"
        assert fresh.attempts == 1

    def test_resume_exhausted_from_backoff_owes_nothing(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_claimed("gh:issue:7", now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        store.mark_exhausted("gh:issue:7", "x", now=3.0, not_before=500.0)
        fresh = store.resume_exhausted("gh:issue:7", "r1", 4.0, "granted")
        assert fresh.state == "queued" and fresh.not_before is None
        assert fresh.pending_report is None

    def test_resume_exhausted_refuses_a_different_or_running_run(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_claimed("gh:issue:7", now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        with pytest.raises(ValueError, match="not pinned to run r2"):
            store.resume_exhausted("gh:issue:7", "r2", 3.0, "x")
        with pytest.raises(ValueError, match="is running"):
            store.resume_exhausted("gh:issue:7", "r1", 3.0, "x")

    def test_other_transitions_clear_the_clock(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_claimed("gh:issue:7", now=1.0)
        store.mark_running("gh:issue:7", "r1", now=2.0)
        store.mark_exhausted("gh:issue:7", "x", now=3.0, not_before=500.0)
        store.mark_failed("gh:issue:7", "gave up", now=4.0, requeue=True)
        got = store.get("gh:issue:7")
        assert got is not None and got.not_before is None and got.run_id is None

    def test_pre_scheduled_retry_database_migrates_in_place(self, tmp_path: Path) -> None:
        """A raw store from before `not_before` opens, gains the column, and
        its pinned rows dispatch exactly as they did (no clock = no wait)."""
        db = _pre_not_before_db(tmp_path)
        store = DaemonStore(db)
        got = store.get("gh:o/r:issue:3")
        assert got is not None and got.not_before is None and got.run_id == "r_old"
        nxt = store.next_queued(now=2.5, backoff_s=1000.0)
        assert nxt is not None and nxt.item_id == "gh:o/r:issue:3"
        store.mark_exhausted("gh:o/r:issue:3", "x", now=3.0, not_before=900.0)
        assert store.next_queued(now=10.0, backoff_s=0.0) is None
        # Reopening does not re-apply the ALTER.
        store.close()
        again = DaemonStore(db)
        got = again.get("gh:o/r:issue:3")
        assert got is not None and got.not_before == 900.0


class TestClaimPersistence:
    """#530: the claim token is written before the comment, a half-claim is
    findable, and a claim that is not ours leaves no row."""

    def test_claiming_then_claimed_keeps_the_token(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_claiming("gh:issue:7", "a" * 32, now=2.0)
        got = store.get("gh:issue:7")
        assert got is not None and got.claim_token == "a" * 32 and not got.claimed
        assert [i.item_id for i in store.half_claimed()] == ["gh:issue:7"]
        store.mark_claimed("gh:issue:7", now=3.0)
        got = store.get("gh:issue:7")
        assert got is not None and got.claimed and got.claim_token == "a" * 32
        assert store.half_claimed() == []

    def test_clear_claim_forgets_the_token(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        store.mark_claiming("gh:issue:7", "a" * 32, now=2.0)
        store.clear_claim("gh:issue:7", now=3.0)
        got = store.get("gh:issue:7")
        assert got is not None and got.claim_token is None and not got.claimed
        assert store.half_claimed() == []

    def test_discard_removes_only_a_queued_row(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        store.upsert_new(item(), now=1.0)
        assert store.discard("gh:issue:7") is True
        assert store.get("gh:issue:7") is None
        assert store.discard("gh:issue:7") is False
        # Rediscovery re-creates it fresh.
        assert store.upsert_new(item(), now=5.0) is True
        store.mark_claimed("gh:issue:7", now=5.0)
        store.mark_running("gh:issue:7", "r1", now=6.0)
        assert store.discard("gh:issue:7") is False, "a running item is not discardable"
        assert store.get("gh:issue:7") is not None

    def test_pre_claim_token_database_migrates_in_place(self, tmp_path: Path) -> None:
        db = daemon_db(tmp_path, "pre_claim_token")
        insert_daemon_row(
            db,
            item_id="gh:o/r:issue:3",
            source_key="3",
            title="Three",
            state="queued",
            claimed=1,
            repo="o/r",
        )
        store = DaemonStore(db)
        got = store.get("gh:o/r:issue:3")
        assert got is not None and got.claim_token is None and got.claimed
        assert store.half_claimed() == [], "a completed claim from before the token is not half"
        store.mark_claiming("gh:o/r:issue:3", "d" * 32, now=2.0)
        store.close()
        again = DaemonStore(db)
        assert [i.claim_token for i in again.half_claimed()] == ["d" * 32]

    def test_pre_prior_attempt_database_migrates_and_restarts_by_label(
        self, tmp_path: Path
    ) -> None:
        """#600: a store written before the prior-attempt columns opens,
        keeps its rows (including a cancelled row pinned to an old run) and
        can be restarted by re-adding the trigger label — no repair."""
        db = daemon_db(tmp_path, "pre_prior_attempt")
        insert_daemon_row(
            db,
            item_id="gh:o/r:issue:3",
            source_key="3",
            title="Three",
            body="do the thing",
            state="cancelled",
            claimed=1,
            run_id="r_old",
            attempts=2,
            last_error="cancelled by b",
            repo="o/r",
        )
        store = DaemonStore(db)
        got = store.get("gh:o/r:issue:3")
        assert got is not None and got.state == "cancelled" and got.run_id == "r_old"
        assert store.prior_attempt("gh:o/r:issue:3") is None
        rediscovered = item(
            "3",
            item_id="gh:o/r:issue:3",
            title="Three",
            body="do the thing",
            repo="o/r",
        )
        assert store.upsert_new(rediscovered, now=9.0) is True
        fresh = store.get("gh:o/r:issue:3")
        assert fresh is not None
        assert (fresh.state, fresh.claimed, fresh.run_id, fresh.attempts) == (
            "queued",
            False,
            None,
            0,
        )
        prior = store.prior_attempt("gh:o/r:issue:3")
        assert prior is not None and prior.run_id == "r_old"


class TestGatePrompts:
    """Where each backend posted a gate's approval prompt: one row per
    (run, backend), so the console's prompt and Discord's button are
    both found after a restart."""

    def _gate(self, store: DaemonStore, run_id: str = "r1") -> None:
        store.create_merge_gate(run_id, "gh:issue:1", "o/r", 9, "u", None, [], f"t-{run_id}", 1.0)

    def test_prompt_per_backend(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        self._gate(store)
        assert store.gate_prompt("r1", "discord") is None
        store.set_gate_prompt("r1", "42", "555", backend="discord")
        store.set_gate_prompt("r1", "thread:5", "77", backend="local")
        store.set_gate_prompt("r2", "42", "", backend="discord")
        assert store.gate_prompt("r2", "discord") is None, "an empty id is no prompt"
        assert store.gate_prompt("r1", "discord") == ("42", "555")
        assert store.gate_prompt("r1", "local") == ("thread:5", "77")
        assert store.gate_prompt("r1", "slack") is None
        store.set_gate_prompt("r1", None, None, backend="local")
        assert store.gate_prompt("r1", "local") is None
        # The gate row itself no longer carries the prompt.
        gate = store.merge_gate_for("r1")
        assert gate is not None and gate.prompt_message_id is None

    def test_legacy_prompt_columns_are_carried_and_cleared(self, tmp_path: Path) -> None:
        """A gate parked before the prompt table existed recorded the
        prompt on the gate row; it is read into the new table under the
        backend the run's thread used — a Slack daemon's under slack — and
        the row is cleared, so a cleared prompt never comes back and an
        older daemon writing the row again (a rollback) is carried again."""
        path = tmp_path / "state.db"
        store = DaemonStore(path)
        self._gate(store)
        self._gate(store, "r_slack")
        self._gate(store, "r_bare")
        store.record_chat_thread("r1", "42", "4242", "100", backend="discord")
        store.record_chat_thread("r_slack", "C1", "17.5", "17.5", backend="slack")
        for run in ("r1", "r_slack", "r_bare"):
            store._conn.execute(
                "UPDATE daemon_merge_gates SET prompt_channel_id = '42', prompt_message_id = '555' "
                "WHERE run_id = ?",
                (run,),
            )
        store._conn.execute("DELETE FROM daemon_gate_prompts")
        store._conn.commit()
        store.close()
        again = DaemonStore(path)
        assert again.gate_prompt("r1", "discord") == ("42", "555")
        assert again.gate_prompt("r_slack", "slack") == ("42", "555")
        assert again.gate_prompt("r_slack", "discord") is None
        # No thread of its own and two backends seen: Discord is the default.
        assert again.gate_prompt("r_bare", "discord") == ("42", "555")
        gate = again.merge_gate_for("r1")
        assert gate is not None and gate.prompt_message_id is None, "the row is cleared"
        again.set_gate_prompt("r1", None, None, backend="discord")
        again.close()
        assert DaemonStore(path).gate_prompt("r1", "discord") is None

    def test_pre_upgrade_watches_follow_the_daemons_backend(self, tmp_path: Path) -> None:
        """A Slack daemon's persisted watches must come back as Slack's,
        not Discord's, or nobody is pinged and the rows leak."""
        path = tmp_path / "state.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE daemon_run_watches (run_id TEXT NOT NULL, watcher_id TEXT NOT NULL,
                created_at REAL NOT NULL, UNIQUE(run_id, watcher_id));
            INSERT INTO daemon_run_watches VALUES ('r1', 'U1', 1.0);
            INSERT INTO daemon_run_watches VALUES ('r2', 'U2', 2.0);
            CREATE TABLE daemon_chat_threads (
                run_id TEXT PRIMARY KEY, backend TEXT NOT NULL DEFAULT 'discord',
                channel_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                headline_id TEXT, status_id TEXT);
            INSERT INTO daemon_chat_threads VALUES ('r1', 'slack', 'C1', '17.5', '17.5', NULL);
            """
        )
        conn.commit()
        conn.close()
        store = DaemonStore(path)
        assert store.all_run_watches("slack") == {"r1": ["U1"], "r2": ["U2"]}
        assert store.all_run_watches("discord") == {}


class TestPendingClarifications:
    """The persisted half of ask-never-block: a filing-blocking question's
    fallback survives a restart and fires exactly once."""

    def _create(self, store: DaemonStore, *, asker: str = "u1", deadline: float = 100.0) -> int:
        row_id = store.create_pending_clarification(
            backend="discord",
            channel_id="42",
            asker_id=asker,
            asker_name="brett",
            question="What are you seeing?",
            assumption="grey cards",
            deadline=deadline,
            now=50.0,
        )
        assert row_id is not None
        return row_id

    def test_create_and_read_back(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        self._create(store)
        (row,) = store.open_clarifications()
        assert row.asker_id == "u1" and row.assumption == "grey cards"
        assert row.state == "open" and row.deadline == 100.0

    def test_take_due_is_a_cas_and_fires_once(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        self._create(store, deadline=100.0)
        self._create(store, asker="u2", deadline=999.0)
        taken = store.take_due_clarifications(now=200.0)
        assert [r.asker_id for r in taken] == ["u1"], "only the due row is claimed"
        assert store.take_due_clarifications(now=200.0) == [], "a second sweep gets nothing"

    def test_sweeps_are_scoped_to_a_backend(self, tmp_path: Path) -> None:
        """Every bridge runs its own sweeper; a Slack ask must not be fired
        by the Discord bridge (which cannot ping the asker) — a sweep with a
        backend takes only its own rows, a bare sweep takes them all."""
        store = DaemonStore(tmp_path / "state.db")
        self._create(store, asker="d1", deadline=10.0)
        assert (
            store.create_pending_clarification(
                backend="slack",
                channel_id="C1",
                asker_id="s1",
                asker_name="s",
                question="q",
                assumption="a",
                deadline=10.0,
                now=1.0,
            )
            is not None
        )
        assert [r.asker_id for r in store.open_clarifications("slack")] == ["s1"]
        assert [r.asker_id for r in store.take_due_clarifications(now=20.0, backend="slack")] == [
            "s1"
        ]
        assert [r.asker_id for r in store.take_due_clarifications(now=20.0)] == ["d1"]

    def test_any_engagement_from_the_asker_resolves_their_rows(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        self._create(store, asker="u1")
        self._create(store, asker="u2")
        assert store.resolve_open_clarifications_for("u1", "42", now=60.0) == 1
        askers = [r.asker_id for r in store.open_clarifications()]
        assert askers == ["u2"], "another asker's question stays armed"

    def test_resolution_is_scoped_to_the_surface_when_known(self, tmp_path: Path) -> None:
        store = DaemonStore(tmp_path / "state.db")
        self._create(store, asker="u1")  # channel 42
        assert store.resolve_open_clarifications_for("u1", "77", now=60.0) == 0
        assert store.resolve_open_clarifications_for("u1", None, now=60.0) == 1

    def test_open_rows_are_capped(self, tmp_path: Path) -> None:
        from sbxloop.daemon.store import PENDING_CLARIFICATION_CAP

        store = DaemonStore(tmp_path / "state.db")
        for i in range(PENDING_CLARIFICATION_CAP):
            self._create(store, asker=f"u{i}")
        over = store.create_pending_clarification(
            backend="discord",
            channel_id="42",
            asker_id="late",
            asker_name=None,
            question="q",
            assumption="a",
            deadline=100.0,
            now=50.0,
        )
        assert over is None
        assert len(store.open_clarifications()) == PENDING_CLARIFICATION_CAP

    def test_a_raw_pre_change_database_upgrades_in_place(self, tmp_path: Path) -> None:
        """The table auto-creates on open; rows an older daemon wrote are
        untouched (the house migration rule: start from a raw pre-change
        database, never one the new code wrote)."""
        db = tmp_path / "state.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            # The pre-change shape, verbatim (no daemon_pending_clarifications
            # table; notably no `kind` column, which the store reads as the
            # pre-1.0 signature and refuses).
            "CREATE TABLE daemon_work_items (item_id TEXT PRIMARY KEY, "
            "source_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, "
            "body TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', "
            "state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
            "claimed INTEGER NOT NULL DEFAULT 0, run_id TEXT, last_error TEXT, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
            "pending_report TEXT, requested_by TEXT);"
            "CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO daemon_state (key, value) VALUES ('k', 'v');"
        )
        conn.commit()
        conn.close()
        store = DaemonStore(db)
        assert store.open_clarifications() == []
        self._create(store)
        assert len(store.open_clarifications()) == 1
        assert store.get_value("k") == "v", "pre-change rows are intact"
