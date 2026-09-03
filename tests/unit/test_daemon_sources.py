"""The GitHub issue source against a recording GithubOps stub."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import unquote

import pytest

from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.daemon.sources import CLAIM_MARKER, GitHubIssueSource, GitHubLabels
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import IssueRef

LABELS = GitHubLabels("sbxloop:run", "sbxloop:in-progress", "sbxloop:failed")


def report(**overrides: Any) -> RunReport:
    fields: dict[str, Any] = {
        "run_id": "r1",
        "state": "merged",
        "task_summary": "2/2 tasks done",
        "pr": (9, "https://x/pull/9"),
    }
    fields.update(overrides)
    return RunReport(**fields)


def gh(number: int = 4, **overrides: Any) -> WorkItem:
    fields: dict[str, Any] = {"item_id": f"gh:{number}", "source_key": str(number), "title": "x"}
    fields.update(overrides)
    return WorkItem(**fields)


class RecordingOps:
    """GithubOps stand-in for the issue source: scripted GET, recorded writes."""

    def __init__(self, issues: dict[str, dict[str, Any]] | None = None) -> None:
        self.issues = issues or {}
        self.searches: list[str] = []
        self.raw_calls: list[tuple[str, str, Any]] = []
        self.comments: list[tuple[int, str]] = []
        self.created: list[tuple[str, list[str] | None]] = []
        self.fail_on: set[str] = set()
        # Comments as GitHub lists them (ascending), events per issue; a
        # claim test scripts a rival's claim comment here.
        self.comment_rows: list[dict[str, Any]] = []
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.deleted_comments: list[int] = []
        # Called after each posted claim comment: lets a test interleave a
        # rival between our post and our re-read.
        self.after_comment: Any = None
        self._next_comment_id = 100

    def search_issues(self, query: str, per_page: int = 30) -> list[dict[str, Any]]:
        self.searches.append(query)
        wanted = None
        if 'label:"' in query:
            wanted = query.split('label:"', 1)[1].split('"', 1)[0]
        return [
            i
            for i in self.issues.values()
            if wanted is None
            or any(
                (lb.get("name") if isinstance(lb, dict) else lb) == wanted
                for lb in i.get("labels") or []
            )
        ]

    def raw(self, method: str, path: str, body: Any = None) -> Any:
        if method in self.fail_on:
            raise GithubOpsError(f"{method} {path} -> HTTP 500: boom")
        self.raw_calls.append((method, path, body))
        base, _, query = path.partition("?")
        if method == "GET" and base.endswith("/comments"):
            return [] if "page=2" in query else list(self.comment_rows)
        if method == "GET" and base.endswith("/events"):
            number = base.rsplit("/", 2)[-2]
            return [] if "page=2" in query else list(self.events.get(number, []))
        if method == "DELETE" and "/issues/comments/" in base:
            self.deleted_comments.append(int(base.rsplit("/", 1)[-1]))
            return {}
        # Label writes actually move the labels, as GitHub does: a claimed
        # issue no longer carries the trigger label and so drops out of
        # discovery until a human re-adds it (#600).
        if method == "POST" and base.endswith("/labels"):
            number = base.rsplit("/", 2)[-2]
            target = self.issues.get(number)
            if target is not None:
                names = {lb["name"] for lb in target.get("labels") or []}
                names.update((body or {}).get("labels") or [])
                target["labels"] = [{"name": n} for n in sorted(names)]
            return {}
        if method == "DELETE" and "/labels/" in base:
            number, _, name = base.rpartition("/labels/")
            number = number.rsplit("/", 1)[-1]
            target = self.issues.get(number)
            if target is not None:
                gone = unquote(name)
                target["labels"] = [lb for lb in target.get("labels") or [] if lb["name"] != gone]
            return {}
        if method == "GET":
            number = base.rsplit("/", 1)[-1]
            return self.issues.get(number, {"state": "closed", "labels": []})
        return {}

    def add_comment(self, body: str, created_at: str = "2026-08-15T10:00:00Z") -> int:
        cid = self._next_comment_id
        self._next_comment_id += 1
        self.comment_rows.append({"id": cid, "body": body, "created_at": created_at})
        return cid

    def issue_comment(self, repo: str, number: int, body: str) -> str:
        if "COMMENT" in self.fail_on:
            raise GithubOpsError("comment -> HTTP 502")
        self.comments.append((number, body))
        self.add_comment(body)
        if self.after_comment is not None:
            self.after_comment()
        return "https://c"

    def issue_create(self, repo: str, title: str, body: str = "", labels: Any = None) -> IssueRef:
        self.created.append((title, labels))
        return IssueRef(number=77, url="https://x/issues/77")


def issue(number: int, *labels: str, state: str = "open") -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "please do it",
        "html_url": f"https://x/issues/{number}",
        "state": state,
        "labels": [{"name": name} for name in labels],
    }


# The fixtures' comment timestamps are 2026-08-15T10:00:00Z; a source built
# on the real clock would read every scripted rival as stale by age (#530).
FIXTURE_NOW = 1786788000.0


class TestGitHubSource:
    def make(self, ops: RecordingOps) -> GitHubIssueSource:
        return GitHubIssueSource(
            lambda: ops,  # type: ignore[arg-type]
            "o/r",
            LABELS,
            host="db",
            clock=lambda: FIXTURE_NOW,
        )

    def test_poll_uses_the_one_trigger_label_query(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run"), "5": issue(5, "sbxloop:failed")})
        items = self.make(ops).poll()
        assert ops.searches == ['repo:o/r is:issue is:open label:"sbxloop:run"']
        assert [i.item_id for i in items] == ["gh:issue:4"]
        assert items[0].url == "https://x/issues/4" and items[0].body == "please do it"
        assert items[0].requested_by is None

    def test_labels_default_the_landing_marks(self) -> None:
        assert LABELS.completed == "sbxloop:completed" and LABELS.blocked == "sbxloop:blocked"

    def test_claim_reverifies_then_swaps_labels_and_comments(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        assert self.make(ops).claim(item) is True
        methods = [(m, p) for m, p, _ in ops.raw_calls]
        assert ("GET", "/repos/o/r/issues/4") in methods
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Arun") in methods
        assert ("POST", "/repos/o/r/issues/4/labels") in methods
        assert ops.comments and "claimed" in ops.comments[0][1] and "`db`" in ops.comments[0][1]

    def test_claim_adds_in_progress_before_removing_trigger(self) -> None:
        """Both labels present is the safe intermediate: a crash between the
        two mutations leaves the trigger, so polling still finds the item."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        assert self.make(ops).claim(item) is True
        mutations = [(m, p) for m, p, _ in ops.raw_calls if m in ("POST", "DELETE")]
        assert mutations == [
            ("POST", "/repos/o/r/issues/4/labels"),
            ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Arun"),
        ]

    def test_claim_clears_the_previous_attempts_lifecycle_labels(self) -> None:
        """#600: a restart-by-label arrives wearing the last attempt's
        failed/blocked/completed mark. Those describe a run that is over, so
        the claim clears them and leaves the issue exactly as a first-time
        claim would — in-progress and nothing else. Labels the daemon does
        not own are still a human's business and stay put."""
        ops = RecordingOps(
            {"4": issue(4, "sbxloop:run", "sbxloop:blocked", "sbxloop:failed", "needs-design")}
        )
        item = self.make(ops).poll()[0]
        assert self.make(ops).claim(item) is True
        deletes = [unquote(p.rsplit("/", 1)[-1]) for m, p, _ in ops.raw_calls if m == "DELETE"]
        assert deletes == ["sbxloop:failed", "sbxloop:blocked", "sbxloop:run"]
        assert {lb["name"] for lb in ops.issues["4"]["labels"]} == {
            "sbxloop:in-progress",
            "needs-design",
        }

    def test_claim_failure_after_adding_in_progress_rolls_it_back(self) -> None:
        """If removing the trigger fails, in-progress must come back off so
        the issue is exactly as found (review: otherwise the item is lost —
        polling only looks for the trigger)."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        deletes: list[str] = []
        original_raw = ops.raw

        def raw(method: str, path: str, body: object = None) -> object:
            if method == "DELETE":
                deletes.append(path)
                if path.endswith("sbxloop%3Arun"):
                    raise GithubOpsError("DELETE trigger -> HTTP 502")
                ops.raw_calls.append((method, path, body))
                return {}
            return original_raw(method, path, body)

        ops.raw = raw  # type: ignore[method-assign]
        assert self.make(ops).claim(item) is False
        # in-progress was added, then rolled back
        assert (
            "POST",
            "/repos/o/r/issues/4/labels",
            {"labels": ["sbxloop:in-progress"]},
        ) in ops.raw_calls
        assert any(d.endswith("sbxloop%3Ain-progress") for d in deletes)
        # the claim comment (the lock) is released too
        assert any(d.endswith("/issues/comments/100") for d in deletes)

    def test_claim_tolerates_structured_404_on_trigger_removal(self) -> None:
        """#221: an already-absent trigger label is signalled by http_status,
        not by "HTTP 404" appearing in the message."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        original_raw = ops.raw

        def raw(method: str, path: str, body: object = None) -> object:
            if method == "DELETE" and path.endswith("sbxloop%3Arun"):
                raise GithubOpsError("label already gone", http_status=404)
            return original_raw(method, path, body)

        ops.raw = raw  # type: ignore[method-assign]
        item = self.make(ops).poll()[0]
        assert self.make(ops).claim(item) is True

    def test_claim_comment_failure_fails_the_claim_without_touching_labels(self) -> None:
        """The claim comment is the lock (#254): if it cannot be posted the
        claim did not happen, and the issue is left exactly as found."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        ops.fail_on = {"COMMENT"}
        item = self.make(ops).poll()[0]
        assert self.make(ops).claim(item) is False
        assert all(m == "GET" for m, _, _ in ops.raw_calls)

    def test_claim_comment_is_posted_before_labels_and_carries_marker(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        assert self.make(ops).claim(item) is True
        assert ops.comments[0][1].startswith(CLAIM_MARKER)
        # comment (lock) → re-read comments → label swap
        kinds = [(m, p.split("?")[0].rsplit("/", 1)[-1]) for m, p, _ in ops.raw_calls]
        assert kinds.index(("GET", "comments")) < kinds.index(("POST", "labels"))

    def test_claim_lost_race_yields_and_releases_its_comment(self) -> None:
        """Two daemons interleaving between the re-GET and the label swap
        both used to claim (#254). With the comment lock the one whose
        claim comment is not first backs off — labels untouched, its own
        comment removed so it does not lock anyone else out."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        rival = f"{CLAIM_MARKER}{'b' * 32} -->\nsbxloop daemon claimed this issue (host `other`)."
        # The rival posted a moment earlier; GitHub lists it first.
        ops.comment_rows.append({"id": 50, "body": rival, "created_at": "2026-08-15T09:59:59Z"})
        assert self.make(ops).claim(item) is False
        assert not any(m in ("POST", "PUT", "PATCH") for m, p, _ in ops.raw_calls if "labels" in p)
        assert ops.deleted_comments == [100]  # ours, not the rival's

    def test_claim_same_second_race_breaks_ties_on_comment_id(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        rival = f"{CLAIM_MARKER}{'c' * 32} -->\nclaimed (host `other`)."

        def rival_lands_after_ours() -> None:
            ops.add_comment(rival)  # same created_at, higher id

        ops.after_comment = rival_lands_after_ours
        assert self.make(ops).claim(item) is True
        assert ops.deleted_comments == []

    def test_claim_ignores_claim_comments_from_an_earlier_trigger_cycle(self) -> None:
        """A re-triggered issue (failed label removed, trigger re-added)
        carries the claim comment of its earlier run; that must not lock
        every future claimer out. Only claims since the trigger label was
        last added count."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        stale = f"{CLAIM_MARKER}{'d' * 32} -->\nclaimed (host `db`)."
        ops.comment_rows.append({"id": 10, "body": stale, "created_at": "2026-08-01T00:00:00Z"})
        ops.events["4"] = [
            {
                "event": "labeled",
                "label": {"name": "sbxloop:run"},
                "created_at": "2026-07-30T00:00:00Z",
            },
            {
                "event": "labeled",
                "label": {"name": "sbxloop:failed"},
                "created_at": "2026-08-02T00:00:00Z",
            },
            {
                "event": "labeled",
                "label": {"name": "sbxloop:run"},
                "created_at": "2026-08-10T00:00:00Z",
            },
        ]
        item = self.make(ops).poll()[0]
        assert self.make(ops).claim(item) is True

    def test_claim_label_swap_failure_releases_the_comment_lock(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        ops.fail_on = {"POST"}  # in-progress add fails
        assert self.make(ops).claim(item) is False
        assert ops.deleted_comments == [100]

    def test_claim_403_on_the_label_write_names_the_label_permission(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#630: a triage-only token reads and comments fine, then 403s on
        the in-progress label. The failure must say which permission is
        missing rather than surface as a bare 403 on an unexpected request;
        the claim still fails closed and rolls back its comment."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        real_raw = ops.raw

        def raw(method: str, path: str, body: Any = None) -> Any:
            if method == "POST" and path.endswith("/labels"):
                raise GithubOpsError("POST -> forbidden: Resource not accessible", http_status=403)
            return real_raw(method, path, body)

        ops.raw = raw  # type: ignore[method-assign]
        failures: list[BaseException] = []
        src = GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db", on_failure=failures.append)  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING):
            assert src.claim(item) is False
        assert ops.deleted_comments == [100]
        (failure,) = failures
        assert isinstance(failure, GithubOpsError) and failure.http_status == 403
        text = str(failure)
        assert "`sbxloop:in-progress`" in text and "o/r#4" in text
        assert "permission to write issue labels" in text
        assert "Issues → read and write" in text and "triage-only" in text
        record = next(r for r in caplog.records if "github.claim_failed" in r.getMessage())
        assert "permission to write issue labels" in record.getMessage()

    def test_claim_non_403_label_failure_passes_through_unchanged(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        ops.fail_on = {"POST"}
        failures: list[BaseException] = []
        src = GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db", on_failure=failures.append)  # type: ignore[arg-type]
        assert src.claim(item) is False
        (failure,) = failures
        assert "HTTP 500: boom" in str(failure) and "permission" not in str(failure)

    def test_poll_raises_and_reports_failure_to_the_sandbox_owner(self) -> None:
        """poll used to swallow failures as an empty result; the loop needs
        the exception to back the source off (#254), and DaemonGithub
        needs to hear about it to replace a dead sandbox."""
        ops = RecordingOps()
        failures: list[BaseException] = []

        def search(query: str, per_page: int = 30) -> list[dict[str, Any]]:
            raise GithubOpsError("HTTP 502")

        ops.search_issues = search  # type: ignore[method-assign]
        src = GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db", on_failure=failures.append)  # type: ignore[arg-type]
        with pytest.raises(GithubOpsError):
            src.poll()
        assert len(failures) == 1

    def test_claim_refuses_when_label_gone_or_issue_closed(self) -> None:
        stale = RecordingOps({"4": issue(4)})  # trigger label already removed
        assert self.make(stale).claim(gh()) is False
        assert all(m == "GET" for m, _, _ in stale.raw_calls)  # no mutations
        closed = RecordingOps({"4": issue(4, "sbxloop:run", state="closed")})
        assert self.make(closed).claim(gh()) is False

    def test_report_merged_labels_then_closes(self) -> None:
        """The merge settles the issue: completed label on, in-progress off,
        closed as completed — labels before the close, so a mid-way failure
        leaves an open, correctly labelled issue."""
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        assert self.make(ops).report_merged(gh(), 9, "https://x/pull/9") is True
        assert any("pull/9" in body and "merged" in body for _, body in ops.comments)
        writes = [(m, p) for m, p, _ in ops.raw_calls if m in {"POST", "PATCH", "DELETE"}]
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Ain-progress") in writes
        completed = writes.index(("POST", "/repos/o/r/issues/4/labels"))
        closed = writes.index(("PATCH", "/repos/o/r/issues/4"))
        assert completed < closed
        assert (
            "POST",
            "/repos/o/r/issues/4/labels",
            {"labels": ["sbxloop:completed"]},
        ) in ops.raw_calls
        assert (
            "PATCH",
            "/repos/o/r/issues/4",
            {"state": "closed", "state_reason": "completed"},
        ) in ops.raw_calls

    def test_report_merged_without_a_pr_number_still_closes(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        assert self.make(ops).report_merged(gh(), None, "") is True
        assert "its pull request was merged" in ops.comments[-1][1]

    def test_report_merged_failure_returns_false_for_a_retry(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        ops.fail_on = {"PATCH"}
        assert self.make(ops).report_merged(gh(), 9, "u") is False

    def test_report_blocked_labels_and_leaves_the_issue_open(self) -> None:
        """GitHub refused to let the loop finish: blocked label on,
        in-progress off, the issue left open with the reason and what a
        human can do about it."""
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        ok = self.make(ops).report_blocked(
            gh(), "a protection rule wants an approval", 9, "https://x/pull/9"
        )
        assert ok is True
        assert not any(m == "PATCH" for m, _, _ in ops.raw_calls)
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Ain-progress", None) in ops.raw_calls
        assert (
            "POST",
            "/repos/o/r/issues/4/labels",
            {"labels": ["sbxloop:blocked"]},
        ) in ops.raw_calls
        body = ops.comments[-1][1]
        # The way back in is the trigger label, not an operator command
        # (#600): re-adding it restarts the item on this same branch/PR.
        assert "protection rule" in body and "pull/9" in body
        assert "re-add `sbxloop:run`" in body and "!sbx retry" not in body
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Arun", None) in ops.raw_calls

    def test_report_blocked_failure_returns_false(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        ops.fail_on = {"POST"}
        assert self.make(ops).report_blocked(gh(), "why", 9, "u") is False

    def test_abandoned_adds_failed_label_with_retrigger_hint(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        self.make(ops).report_abandoned(gh(claimed=True), "budget exhausted")
        assert (
            "POST",
            "/repos/o/r/issues/4/labels",
            {"labels": ["sbxloop:failed"]},
        ) in ops.raw_calls
        assert any("Re-add `sbxloop:run`" in body for _, body in ops.comments)
        # The trigger is cleared unconditionally so the human's re-add fires
        # a fresh label event even if one was still on the issue (#596).
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Arun", None) in ops.raw_calls

    def test_abandoned_while_unclaimed_drops_the_trigger_label(self) -> None:
        """#229: an operator abandons an item that was never claimed (still
        queued). The trigger label is what is on the issue; left there the
        issue reads as work to do and "re-add the trigger" is a no-op."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        self.make(ops).report_abandoned(gh(claimed=False), "abandoned by operator")
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Arun", None) in ops.raw_calls
        assert (
            "POST",
            "/repos/o/r/issues/4/labels",
            {"labels": ["sbxloop:failed"]},
        ) in ops.raw_calls

    def test_cancelled_removes_in_progress_and_leaves_trigger_to_a_human(self) -> None:
        """#246: a cancel is neither failure nor trigger — the issue is left
        unlabeled with a comment saying who cancelled and how to continue."""
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        cancelled = report(state="cancelled", pr=None, cancelled_by="Discord user `b`")
        self.make(ops).report_cancelled(gh(), cancelled)
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Ain-progress", None) in ops.raw_calls
        assert not any(m == "POST" for m, _, _ in ops.raw_calls)  # no failed label
        body = ops.comments[-1][1]
        assert "cancelled by Discord user `b`" in body
        assert "`sbxloop resume r1`" in body and "!sbx retry gh:issue:4" in body

    def test_cancelled_with_requeue_keeps_in_progress(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        self.make(ops).report_cancelled(gh(), report(state="cancelled", requeued=True))
        assert not any(m == "DELETE" for m, _, _ in ops.raw_calls)
        assert "Re-queued" in ops.comments[-1][1]

    def test_requeued_reclaims_with_in_progress_and_drops_failed(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:failed")})
        self.make(ops).report_requeued(gh(), "Discord user `b`")
        assert (
            "POST",
            "/repos/o/r/issues/4/labels",
            {"labels": ["sbxloop:in-progress"]},
        ) in ops.raw_calls
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Afailed", None) in ops.raw_calls
        assert "Re-queued by Discord user `b`" in ops.comments[-1][1]

    def test_requeued_strips_blocked_and_completed_before_claiming(self) -> None:
        """An operator re-queue of a blocked or done item must not leave the
        issue wearing two lifecycle labels: the stale ones go first, so a
        swallowed failure cannot leave both behind."""
        ops = RecordingOps({"4": issue(4, "sbxloop:blocked")})
        self.make(ops).report_requeued(gh(), "b")
        paths = [(m, p) for m, p, _ in ops.raw_calls if m in {"DELETE", "POST"}]
        for stale in ("failed", "blocked", "completed"):
            gone = paths.index(("DELETE", f"/repos/o/r/issues/4/labels/sbxloop%3A{stale}"))
            assert gone < paths.index(("POST", "/repos/o/r/issues/4/labels"))
        # a failing label removal must not leave in-progress added on top
        ops = RecordingOps({"4": issue(4, "sbxloop:blocked")})
        ops.fail_on = {"DELETE"}
        self.make(ops).report_requeued(gh(), "b")
        assert not any(m == "POST" for m, _, _ in ops.raw_calls)

    def test_reporting_failures_are_swallowed(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        ops.fail_on = {"COMMENT", "PATCH", "DELETE", "POST"}
        src = self.make(ops)
        src.report_started(gh(), "r1")  # must not raise
        assert src.report_merged(gh(), 9, "u") is False
        assert src.report_blocked(gh(), "why", 9, "u") is False
        src.report_retry(gh(), "err", 1)
        src.report_abandoned(gh(), "err")
        src.report_cancelled(gh(), report(state="cancelled"))
        src.report_requeued(gh(), "b")

    def test_the_source_files_nothing(self) -> None:
        """No lane writes issues any more: the source has no file_* surface,
        and a full lifecycle creates no issue."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        src = self.make(ops)
        assert not any(name.startswith("file_") for name in dir(src))
        item = src.poll()[0]
        src.claim(item)
        src.report_started(item, "r1")
        src.report_merged(item, 9, "u")
        assert ops.created == []


class TestGitHubSourceLogging:
    """The poll and claim protocol narrate themselves: a lost race and a
    successful claim are both INFO lines carrying the item; polls are DEBUG."""

    def make(self, ops: RecordingOps) -> GitHubIssueSource:
        return GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db")  # type: ignore[arg-type]

    def test_poll_and_claim_are_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        with caplog.at_level(logging.DEBUG, logger="sbxloop.daemon.sources"):
            item = self.make(ops).poll()[0]
            assert self.make(ops).claim(item) is True
        messages = [r.getMessage() for r in caplog.records]
        polled = [m for m in messages if "'event': 'github.polled'" in m]
        assert polled and "'issues': 1" in polled[0]
        (claimed,) = [m for m in messages if "'event': 'github.claimed'" in m]
        assert "'item': 'gh:issue:4'" in claimed and "'duration_s'" in claimed

    def test_claim_declined_when_trigger_gone_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        ops.issues["4"]["labels"] = []  # someone else swapped the label meanwhile
        with caplog.at_level(logging.INFO, logger="sbxloop.daemon.sources"):
            assert self.make(ops).claim(item) is False
        (declined,) = [
            r.getMessage()
            for r in caplog.records
            if "'event': 'github.claim_declined'" in r.getMessage()
        ]
        assert "'item': 'gh:issue:4'" in declined and "trigger label gone" in declined


class TestDaemonGithubInstance:
    def test_sandbox_name_is_per_state_dir(self, tmp_path: Any) -> None:
        """A fixed name plus remove_stale() at startup meant a second daemon
        on the host killed the first's github sandbox (#254)."""
        from sbxloop.config import Config
        from sbxloop.daemon.github import SANDBOX_NAME_PREFIX, DaemonGithub, sandbox_name_for
        from sbxloop.events import EventBus

        a = Config.model_validate({"state_dir": str(tmp_path / "a")})
        b = Config.model_validate({"state_dir": str(tmp_path / "b")})
        gh_a = DaemonGithub(a, sbx=object(), bus=EventBus(), worker_python="python3")  # type: ignore[arg-type]
        gh_b = DaemonGithub(b, sbx=object(), bus=EventBus(), worker_python="python3")  # type: ignore[arg-type]
        assert gh_a.name != gh_b.name
        assert gh_a.name.startswith(SANDBOX_NAME_PREFIX + "-")
        assert gh_a.name == sandbox_name_for(a.state_dir)  # stable across restarts

    def test_reprovision_is_rate_limited(self, tmp_path: Any) -> None:
        """A GitHub outage used to cost one microVM rebuild per failing
        call; now at most one per REPROVISION_MIN_INTERVAL_S (#254)."""
        from sbxloop.config import Config
        from sbxloop.daemon.github import REPROVISION_MIN_INTERVAL_S, DaemonGithub
        from sbxloop.events import EventBus

        config = Config.model_validate({"state_dir": str(tmp_path / "state")})
        now = [1000.0]
        gh_ = DaemonGithub(
            config,
            sbx=object(),
            bus=EventBus(),
            worker_python="python3",
            clock=lambda: now[0],  # type: ignore[arg-type]
        )
        provisions = 0

        def provision() -> object:
            nonlocal provisions
            provisions += 1
            return object()

        gh_._provision = provision  # type: ignore[method-assign]
        gh_.ops()
        assert provisions == 1
        assert gh_.note_failure(GithubOpsError("HTTP 502")) is True
        gh_.ops()
        assert provisions == 2
        now[0] += 10
        assert gh_.note_failure(GithubOpsError("HTTP 502")) is False
        gh_.ops()
        assert provisions == 2  # still the same sandbox
        now[0] += REPROVISION_MIN_INTERVAL_S
        assert gh_.note_failure(GithubOpsError("HTTP 502")) is True
        gh_.ops()
        assert provisions == 3

    def test_call_retries_once_after_reprovision_and_raises_when_throttled(
        self, tmp_path: Any
    ) -> None:
        from sbxloop.config import Config
        from sbxloop.daemon.github import DaemonGithub
        from sbxloop.events import EventBus

        config = Config.model_validate({"state_dir": str(tmp_path / "state")})
        gh_ = DaemonGithub(
            config, sbx=object(), bus=EventBus(), worker_python="python3", clock=lambda: 0.0
        )  # type: ignore[arg-type]
        gh_._provision = lambda: object()  # type: ignore[method-assign, assignment]
        calls = 0

        def flaky(_ops: object) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise GithubOpsError("boom")
            return "ok"

        assert gh_.call(flaky) == "ok" and calls == 2
        with pytest.raises(GithubOpsError):
            gh_.call(lambda _ops: (_ for _ in ()).throw(GithubOpsError("again")))


class TestDaemonGithubProvisioning:
    def test_provision_error_is_wrapped_as_daemon_error(self, tmp_path: Any) -> None:
        """ensure_github_only can raise ProvisionError (not just SbxError);
        both must surface as one DaemonError (review)."""
        from sbxloop.config import Config
        from sbxloop.daemon.github import DaemonGithub
        from sbxloop.errors import DaemonError, ProvisionError
        from sbxloop.events import EventBus

        config = Config.model_validate({"state_dir": str(tmp_path / "state")})
        gh_ = DaemonGithub(config, sbx=object(), bus=EventBus(), worker_python="python3")  # type: ignore[arg-type]

        class Boom:
            def ensure_github_only(self, *a: object, **k: object) -> object:
                raise ProvisionError("GH_TOKEN is not set")

        gh_.provisioner = Boom()  # type: ignore[assignment]
        with pytest.raises(DaemonError, match="GH_TOKEN"):
            gh_.ops()


class TestTypedItemIds:
    """Freshly discovered issues are minted with the typed id grammar."""

    def test_polled_items_carry_typed_ids(self) -> None:
        ops = RecordingOps({"12": issue(12, "sbxloop:run")})
        source = GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db")  # type: ignore[arg-type]
        items = source.poll()
        assert [i.item_id for i in items] == ["gh:issue:12"]
        assert [i.source_key for i in items] == ["12"]


class TestClaimStaleness:
    """#530: a claim comment from a process that is gone is not a live
    claim — it is released and the race judged among the live ones."""

    NOW = 1_700_000_000.0  # 2023-11-14T22:13:20Z

    def make(self, ops: RecordingOps, *, alive: bool = True, stale_after_s: float = 300.0):
        return GitHubIssueSource(
            lambda: ops,  # type: ignore[arg-type]
            "o/r",
            LABELS,
            host="db",
            pid=4242,
            alive=lambda pid: alive,
            clock=lambda: self.NOW,
            stale_after_s=stale_after_s,
        )

    @staticmethod
    def rival(
        token: str, *, host: str = "db", pid: int = 99, started: str = "2023-11-14T22:13:00Z"
    ) -> str:
        return (
            f"{CLAIM_MARKER}{token} host={host} pid={pid} started={started} -->\n"
            "sbxloop daemon claimed this issue."
        )

    def test_the_claim_comment_carries_host_pid_and_start(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0].model_copy(update={"claim_token": "f" * 32})
        assert self.make(ops).claim(item) is True
        ((_, body),) = ops.comments
        assert body.startswith(
            f"{CLAIM_MARKER}{'f' * 32} host=db pid=4242 started=2023-11-14T22:13:20Z -->"
        )

    def test_a_dead_pid_on_this_host_is_reclaimed(self) -> None:
        """The 2026-08-29 21:10Z sequence: the old process's comment sits
        first; the new process would have conceded to its own predecessor."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        ops.comment_rows.append(
            {"id": 50, "body": self.rival("b" * 32, pid=99), "created_at": "2023-11-14T22:13:10Z"}
        )
        assert self.make(ops, alive=False).claim(item) is True
        assert ops.deleted_comments == [50], "the stale claim is released, ours stands"
        assert any("labels" in p and m == "POST" for m, p, _ in ops.raw_calls)

    def test_a_live_pid_on_this_host_wins_the_race(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        ops.comment_rows.append(
            {"id": 50, "body": self.rival("b" * 32, pid=99), "created_at": "2023-11-14T22:13:10Z"}
        )
        assert self.make(ops, alive=True).claim(item) is False
        assert ops.deleted_comments == [100], "ours is released, the rival's stands"

    def test_an_old_claim_with_no_run_started_is_reclaimed_from_any_host(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        ops.comment_rows.append(
            {
                "id": 50,
                "body": self.rival("b" * 32, host="elsewhere", pid=7),
                "created_at": "2023-11-14T21:00:00Z",  # over an hour ago
            }
        )
        assert self.make(ops, alive=True).claim(item) is True
        assert ops.deleted_comments == [50]

    def test_an_old_claim_whose_run_started_is_live(self) -> None:
        """A stuck run is recovery's problem on that host, not a free issue."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        ops.comment_rows.append(
            {
                "id": 50,
                "body": self.rival("b" * 32, host="elsewhere", pid=7),
                "created_at": "2023-11-14T21:00:00Z",
            }
        )
        ops.comment_rows.append(
            {"id": 51, "body": "Run `rxyz` started.", "created_at": "2023-11-14T21:00:30Z"}
        )
        assert self.make(ops, alive=True).claim(item) is False
        assert ops.deleted_comments == [100]

    def test_a_recent_claim_from_another_host_is_live(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        ops.comment_rows.append(
            {
                "id": 50,
                "body": self.rival("b" * 32, host="elsewhere", pid=7),
                "created_at": "2023-11-14T22:13:10Z",
            }
        )
        assert self.make(ops, alive=True).claim(item) is False

    def test_staleness_by_age_can_be_switched_off(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        ops.comment_rows.append(
            {
                "id": 50,
                "body": self.rival("b" * 32, host="elsewhere", pid=7),
                "created_at": "2023-11-14T21:00:00Z",
            }
        )
        assert self.make(ops, stale_after_s=0).claim(item) is False

    def test_a_claim_comment_without_metadata_still_counts(self) -> None:
        """Comments from before the metadata existed parse, and are judged
        by age alone."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.make(ops).poll()[0]
        old_form = f"{CLAIM_MARKER}{'b' * 32} -->\nclaimed (host `other`)."
        ops.comment_rows.append({"id": 50, "body": old_form, "created_at": "2023-11-14T22:13:10Z"})
        assert self.make(ops).claim(item) is False
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        ops.comment_rows.append({"id": 50, "body": old_form, "created_at": "2023-11-14T21:00:00Z"})
        assert self.make(ops).claim(item) is True
        assert ops.deleted_comments == [50]


class TestSettleClaim:
    """#530: recovery asks the source whether a half-claim's comment landed."""

    def make(self, ops: RecordingOps) -> GitHubIssueSource:
        return GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db")  # type: ignore[arg-type]

    def test_comment_present_finishes_the_label_swap(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        token = "c" * 32
        ops.comment_rows.append(
            {
                "id": 50,
                "body": f"{CLAIM_MARKER}{token} host=db pid=1 started=x -->\nclaimed.",
                "created_at": "2026-08-15T10:00:00Z",
            }
        )
        item = gh(4, claim_token=token)
        assert self.make(ops).settle_claim(item) is True
        writes = [(m, unquote(p.rsplit("/", 1)[-1])) for m, p, _ in ops.raw_calls if "labels" in p]
        assert ("POST", "labels") in writes and ("DELETE", "sbxloop:run") in writes

    def test_comment_present_and_labels_already_swapped_changes_nothing(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        token = "c" * 32
        ops.comment_rows.append(
            {"id": 50, "body": f"{CLAIM_MARKER}{token} -->", "created_at": "2026-08-15T10:00:00Z"}
        )
        assert self.make(ops).settle_claim(gh(4, claim_token=token)) is True
        assert not any("labels" in p for _, p, _ in ops.raw_calls)

    def test_comment_absent_means_nothing_reached_the_source(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        assert self.make(ops).settle_claim(gh(4, claim_token="c" * 32)) is False
        assert self.make(ops).settle_claim(gh(4)) is False
        assert not any("labels" in p for _, p, _ in ops.raw_calls)

    def test_a_github_failure_reads_as_absent(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        ops.fail_on.add("GET")
        assert self.make(ops).settle_claim(gh(4, claim_token="c" * 32)) is False


class TestRelabelRestartsDiscovery:
    """#600: re-adding the trigger label to an unchanged issue is a
    restart request — discovery re-finds it and the store re-queues it."""

    def source(self, ops: RecordingOps) -> GitHubIssueSource:
        return GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db")  # type: ignore[arg-type]

    def store(self, tmp_path: Any) -> Any:
        from sbxloop.daemon.store import DaemonStore

        return DaemonStore(tmp_path / "state.db")

    def test_unchanged_issue_relabelled_is_queued_again(self, tmp_path: Any) -> None:
        ops = RecordingOps({"12": issue(12, "sbxloop:run")})
        store = self.store(tmp_path)
        (first,) = self.source(ops).poll()
        assert store.upsert_new(first, now=1.0) is True
        store.mark_running(first.item_id, "r1", now=2.0)
        store.record_prior_attempt(first.item_id, run_id="r1", branch="sbxloop/gh-12", pr_number=7)
        store.mark_cancelled(first.item_id, "cancelled by b", now=3.0)
        # The human re-adds the label; the issue text is untouched.
        (again,) = self.source(ops).poll()
        assert again.title == first.title and again.body == first.body
        assert store.upsert_new(again, now=4.0) is True
        got = store.get(first.item_id)
        assert got is not None and got.state == "queued" and not got.claimed
        prior = store.prior_attempt(first.item_id)
        assert prior is not None and prior.branch == "sbxloop/gh-12" and prior.pr_number == 7

    def test_a_live_item_is_not_restarted_by_a_poll(self, tmp_path: Any) -> None:
        ops = RecordingOps({"12": issue(12, "sbxloop:run")})
        store = self.store(tmp_path)
        (first,) = self.source(ops).poll()
        store.upsert_new(first, now=1.0)
        store.mark_running(first.item_id, "r1", now=2.0)
        (again,) = self.source(ops).poll()
        assert store.upsert_new(again, now=3.0) is False
        got = store.get(first.item_id)
        assert got is not None and got.state == "running" and got.run_id == "r1"

    def test_an_edited_finished_issue_still_supersedes(self, tmp_path: Any) -> None:
        ops = RecordingOps({"12": issue(12, "sbxloop:run")})
        store = self.store(tmp_path)
        (first,) = self.source(ops).poll()
        store.upsert_new(first, now=1.0)
        store.mark_done(first.item_id, now=2.0)
        ops.issues["12"]["body"] = "please do it, differently"
        (again,) = self.source(ops).poll()
        assert store.upsert_new(again, now=3.0) is True
        got = store.get(first.item_id)
        assert got is not None and got.state == "queued"
        assert got.body == "please do it, differently"

    def test_the_cancel_comment_promises_a_label_restart(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        src = GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db")  # type: ignore[arg-type]
        src.report_cancelled(gh(), report(state="cancelled", pr=None, cancelled_by="`b`"))
        body = ops.comments[-1][1]
        assert "re-add `sbxloop:run`" in body
        assert "deduplicated" not in body and "only re-runs it if the issue was" not in body

    def test_cancel_leaves_the_issue_ready_for_a_relabel(self) -> None:
        """Nothing the cancel report leaves behind may block the restart:
        no in-progress (it is the claim marker), no trigger label (a leftover
        one makes re-adding it a no-op for the human)."""
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress", "sbxloop:run")})
        src = GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db")  # type: ignore[arg-type]
        src.report_cancelled(gh(), report(state="cancelled", pr=None, cancelled_by="`b`"))
        assert {lb["name"] for lb in ops.issues["4"]["labels"]} == set()

    def test_abandon_tells_the_human_to_just_re_add_the_trigger(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        src = GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db")  # type: ignore[arg-type]
        src.report_abandoned(gh(claimed=True), "boom")
        body = ops.comments[-1][1]
        assert "Re-add `sbxloop:run`" in body
        assert "removing" not in body  # no manual label surgery asked of the human

    def test_a_previously_failed_issue_is_reclaimed_by_relabel(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run", "sbxloop:failed")})
        item = (
            self.source(ops)
            .poll()[0]
            .model_copy(
                update={"prior_run_id": "r1", "prior_branch": "sbxloop/gh-4", "prior_pr_number": 9}
            )
        )
        assert self.source(ops).claim(item) is True
        assert {lb["name"] for lb in ops.issues["4"]["labels"]} == {"sbxloop:in-progress"}
        body = ops.comments[-1][1]
        assert "Restarted by re-adding `sbxloop:run`" in body
        assert "`sbxloop:failed`" in body
        assert "branch `sbxloop/gh-4` and PR #9" in body

    def test_a_previously_cancelled_issue_is_reclaimed_by_relabel(self) -> None:
        """A cancel leaves no lifecycle label at all — only the store knows
        it is a restart, and the claim comment must still say so."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = (
            self.source(ops)
            .poll()[0]
            .model_copy(update={"prior_run_id": "r1", "prior_branch": "sbxloop/gh-4"})
        )
        assert self.source(ops).claim(item) is True
        assert {lb["name"] for lb in ops.issues["4"]["labels"]} == {"sbxloop:in-progress"}
        body = ops.comments[-1][1]
        assert "Restarted by re-adding `sbxloop:run`" in body
        assert "branch `sbxloop/gh-4`" in body

    def test_a_restart_with_nothing_on_origin_says_it_starts_fresh(self) -> None:
        """A real restart (the store recorded an earlier run) that pushed
        nothing to origin: still a restart, but honest that it starts over."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run", "sbxloop:blocked")})
        item = self.source(ops).poll()[0].model_copy(update={"prior_run_id": "r1"})
        assert self.source(ops).claim(item) is True
        body = ops.comments[-1][1]
        assert "Restarted by re-adding `sbxloop:run`" in body
        assert "`sbxloop:blocked`" in body
        assert "starting fresh" in body

    def test_a_hand_labelled_issue_is_not_announced_as_a_restart(self) -> None:
        """A human can hand-apply `sbxloop:failed` to an issue that was
        never attempted. Its FIRST claim must not announce itself as a
        restart — the store, not the label, is what knows about a previous
        attempt. The label is only named as something this claim cleared.

        The item is built the way discovery builds it (no prior_* at all),
        not through the re-queue path that would set them."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run", "sbxloop:failed")})
        item = self.source(ops).poll()[0]
        assert item.restarted is False
        assert self.source(ops).claim(item) is True
        body = ops.comments[-1][1]
        assert "Restarted by re-adding" not in body
        assert "starting fresh" not in body
        # the label it cleared is still reported, just not as a restart
        assert "Cleared `sbxloop:failed`" in body
        assert {lb["name"] for lb in ops.issues["4"]["labels"]} == {"sbxloop:in-progress"}

    def test_a_first_claim_says_nothing_about_restarting(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = self.source(ops).poll()[0]
        assert self.source(ops).claim(item) is True
        assert "Restarted" not in ops.comments[-1][1]

    def test_discovery_finds_an_issue_still_wearing_a_failed_label(self) -> None:
        """The search must key on the trigger label alone: an issue whose
        last attempt failed or was blocked still carries that mark when a
        human re-adds the trigger, and excluding it would make the label
        inert (#596)."""
        ops = RecordingOps(
            {
                "4": issue(4, "sbxloop:run", "sbxloop:failed"),
                "5": issue(5, "sbxloop:run", "sbxloop:blocked"),
                "6": issue(6, "sbxloop:failed"),
            }
        )
        items = self.source(ops).poll()
        assert [i.item_id for i in items] == ["gh:issue:4", "gh:issue:5"]
        assert "-label" not in ops.searches[0] and "sbxloop:failed" not in ops.searches[0]
        src = GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db")  # type: ignore[arg-type]
        src.report_cancelled(gh(), report(state="cancelled", pr=None, cancelled_by="`b`"))
        body = ops.comments[-1][1]
        assert "re-add `sbxloop:run`" in body
        assert "deduplicated" not in body and "only re-runs it if the issue was" not in body
