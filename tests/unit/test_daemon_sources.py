"""Work sources against tmp dirs and a recording GithubOps stub."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.daemon.sources import (
    CLAIM_MARKER,
    GitHubIssueSource,
    GitHubLabels,
    InboxSource,
    parse_markdown_item,
)
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import ChecksVerdict, IssueRef

LABELS = GitHubLabels("sbxloop:run", "sbxloop:in-progress", "sbxloop:failed", "sbxloop:backlog")


def report(**overrides: Any) -> RunReport:
    fields: dict[str, Any] = {
        "run_id": "r1",
        "state": "completed",
        "task_summary": "2/2 tasks done",
        "tracking_issue": (5, "https://x/issues/5"),
        "delivery": (9, "https://x/pull/9"),
    }
    fields.update(overrides)
    return RunReport(**fields)


class TestParse:
    def test_heading_becomes_title(self) -> None:
        title, body = parse_markdown_item("# Fix login\n\nbody here\nmore\n", "f")
        assert title == "Fix login"
        assert body == "body here\nmore"

    def test_no_heading_falls_back(self) -> None:
        assert parse_markdown_item("just text", "my-file") == ("my-file", "just text")


class TestInbox:
    def make(self, tmp_path: Path, now: float = 1000.0) -> tuple[InboxSource, Path]:
        clock_val = {"t": now}
        src = InboxSource(tmp_path / "inbox", clock=lambda: clock_val["t"])
        return src, tmp_path / "inbox"

    def _drop(self, root: Path, name: str, text: str, mtime: float) -> Path:
        path = root / "pending" / name
        path.write_text(text)
        os.utime(path, (mtime, mtime))
        return path

    def test_poll_skips_fresh_files_and_parses_settled_ones(self, tmp_path: Path) -> None:
        src, root = self.make(tmp_path, now=1000.0)
        self._drop(root, "fresh.md", "# Fresh\n", mtime=999.5)
        self._drop(root, "old.md", "# Old task\n\ndetails\n", mtime=900.0)
        items = src.poll()
        assert [i.source_key for i in items] == ["old.md"]
        assert items[0].item_id == "inbox:old.md"
        assert items[0].title == "Old task" and items[0].body == "details"

    def test_claim_moves_atomically_and_lost_race_returns_false(self, tmp_path: Path) -> None:
        src, root = self.make(tmp_path)
        self._drop(root, "a.md", "# A\n", mtime=1.0)
        item = src.poll()[0]
        assert src.claim(item) is True
        assert (root / "running" / "a.md").exists()
        assert not (root / "pending" / "a.md").exists()
        assert src.claim(item) is False  # gone from pending now

    def test_success_and_failure_record_results(self, tmp_path: Path) -> None:
        src, root = self.make(tmp_path)
        self._drop(root, "a.md", "# A\n", mtime=1.0)
        item = src.poll()[0]
        src.claim(item)
        src.report_success(item, report())
        assert (root / "done" / "a.md").exists()
        result = (root / "done" / "a.result.md").read_text()
        assert "completed" in result and "pull/9" in result

        self._drop(root, "b.md", "# B\n", mtime=1.0)
        item_b = next(i for i in src.poll() if i.source_key == "b.md")
        src.claim(item_b)
        src.report_abandoned(item_b, "kaboom")
        assert (root / "failed" / "b.md").exists()
        assert "kaboom" in (root / "failed" / "b.result.md").read_text()

    def test_abandoned_while_unclaimed_moves_the_pending_file(self, tmp_path: Path) -> None:
        """#229: an operator abandons an item that was never claimed; the
        file is still in pending/ and must not be left looking like work."""
        src, root = self.make(tmp_path)
        self._drop(root, "c.md", "# C\n", mtime=1.0)
        item = src.poll()[0]
        src.report_abandoned(item, "abandoned by operator")
        assert (root / "failed" / "c.md").exists() and not (root / "pending" / "c.md").exists()
        assert src.poll() == []

    def test_cancelled_lands_in_failed_with_resume_hint_and_requeue_undoes_it(
        self, tmp_path: Path
    ) -> None:
        src, root = self.make(tmp_path)
        self._drop(root, "a.md", "# A\n", mtime=1.0)
        item = src.poll()[0]
        src.claim(item)
        src.report_cancelled(item, report(state="cancelled", cancelled_by="op", requeued=True))
        assert (root / "running" / "a.md").exists()  # --retry: still work
        src.report_cancelled(item, report(state="cancelled", cancelled_by="op"))
        assert (root / "failed" / "a.md").exists()
        note = (root / "failed" / "a.result.md").read_text()
        assert "cancelled by op" in note and "sbxloop resume r1" in note
        src.report_requeued(item, "op")
        assert (root / "running" / "a.md").exists() and not (root / "failed" / "a.md").exists()
        # The stale note goes too, or a later success would show the item as
        # both failed and done.
        assert not (root / "failed" / "a.result.md").exists()

    def test_file_backlog_triage_vs_trigger(self, tmp_path: Path) -> None:
        src, root = self.make(tmp_path)
        ref = src.file_backlog("Add caching", "it is slow", "r7", trigger=False)
        assert ref.startswith("inbox:add-caching-")
        (path,) = list((root / "triage").glob("add-caching-*.md"))
        assert "# Add caching" in path.read_text() and "run r7" in path.read_text()
        src.file_backlog("Add caching", "it is slow", "r7", trigger=True)
        assert list((root / "pending").glob("add-caching-*.md"))


class TestInboxEnqueue:
    def test_enqueue_writes_pending_with_attribution(self, tmp_path: Path) -> None:
        clock = [1000.0]
        src = InboxSource(tmp_path / "inbox", clock=lambda: clock[0])
        ref = src.enqueue("Add caching", "it is slow", by="Discord user `ana` (via concierge)")
        assert ref.startswith("inbox:add-caching-")
        (path,) = list((tmp_path / "inbox" / "pending").glob("add-caching-*.md"))
        text = path.read_text()
        assert text.startswith("# Add caching\n\nit is slow\n")
        assert "Requested by Discord user `ana` (via concierge) via the concierge" in text
        # Asking twice queues twice (the fingerprint includes the clock).
        clock[0] += 1
        ref2 = src.enqueue("Add caching", "it is slow", by="x")
        assert ref2 != ref
        assert len(list((tmp_path / "inbox" / "pending").glob("add-caching-*.md"))) == 2
        # Once settled (mtime older than the settle window against the real
        # clock the source compares with), poll() sees ordinary work items.
        import os
        import time

        for written in (tmp_path / "inbox" / "pending").glob("*.md"):
            os.utime(written, (time.time() - 30, time.time() - 30))
        titles = [i.title for i in InboxSource(tmp_path / "inbox").poll()]
        assert titles == ["Add caching", "Add caching"]


class RecordingOps:
    """GithubOps stand-in for the issue source: scripted GET, recorded writes."""

    def __init__(self, issues: dict[str, dict[str, Any]] | None = None) -> None:
        self.issues = issues or {}
        self.searches: list[str] = []
        self.raw_calls: list[tuple[str, str, Any]] = []
        self.comments: list[tuple[int, str]] = []
        self.created: list[tuple[str, list[str] | None]] = []
        self.created_in: list[str] = []  # repo each issue_create targeted
        self.created_bodies: list[str] = []
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
        # Scripted PR payloads for pr_get, and a call log for the reads the
        # merged/closed short-circuit must NOT spend.
        self.prs: dict[int, dict[str, Any]] = {}
        self.pr_reads: list[str] = []
        # Scripted review rows and inline PR comments for pr_review_feedback.
        self.review_rows: list[dict[str, Any]] = []
        self.pr_comment_rows: list[dict[str, Any]] = []

    def pr_get(self, repo: str, number: int) -> dict[str, Any]:
        self.pr_reads.append("get")
        return self.prs.get(number, {"state": "open", "merged": False, "head": {"sha": "abc"}})

    def pr_checks(self, repo: str, sha: str) -> ChecksVerdict:
        self.pr_reads.append("checks")
        return ChecksVerdict("green", 1, (), ())

    def pr_review_state(self, repo: str, number: int) -> str:
        self.pr_reads.append("reviews")
        return "NONE"

    def search_issues(self, query: str, per_page: int = 30) -> list[dict[str, Any]]:
        self.searches.append(query)
        # Honour the label clause the way GitHub does, so the two-lane poll
        # (trigger label, then audit label) sees each issue in its lane only.
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
        if method == "GET" and "/pulls/" in base and base.endswith("/reviews"):
            return list(self.review_rows)
        if method == "GET" and "/pulls/" in base and base.endswith("/comments"):
            return list(self.pr_comment_rows)
        if method == "GET" and base.endswith("/comments"):
            return [] if "page=2" in query else list(self.comment_rows)
        if method == "GET" and base.endswith("/events"):
            number = base.rsplit("/", 2)[-2]
            return [] if "page=2" in query else list(self.events.get(number, []))
        if method == "DELETE" and "/issues/comments/" in base:
            self.deleted_comments.append(int(base.rsplit("/", 1)[-1]))
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
        self.created_in.append(repo)
        self.created_bodies.append(body)
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


class TestGitHubSource:
    def make(self, ops: RecordingOps) -> GitHubIssueSource:
        return GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db")  # type: ignore[arg-type]

    def test_poll_uses_trigger_label_query(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        items = self.make(ops).poll()
        assert ops.searches == [
            'repo:o/r is:issue is:open label:"sbxloop:run"',
            'repo:o/r is:issue is:open label:"sbxloop:audit"',
        ]
        assert [i.item_id for i in items] == ["gh:4"]
        assert items[0].kind == "patch"
        assert items[0].url == "https://x/issues/4" and items[0].body == "please do it"

    def test_poll_audit_lane_stamps_kind_and_both_labels_read_as_patch(self) -> None:
        """The discovery lane: an issue carrying the audit label is an audit
        item; one carrying both labels is a patch (the safer reading)."""
        ops = RecordingOps(
            {
                "5": issue(5, "sbxloop:audit"),
                "6": issue(6, "sbxloop:run", "sbxloop:audit"),
            }
        )
        items = {i.item_id: i for i in self.make(ops).poll()}
        assert items["gh:5"].kind == "audit"
        assert items["gh:6"].kind == "patch"

    def test_claim_clears_delivered_only_when_present(self) -> None:
        """A blind DELETE of the delivered label 404s on every fresh issue and
        the event stream renders each as an error panel (field noise on
        every audit claim); only clear it when the re-GET showed it."""
        fresh = RecordingOps({"4": issue(4, "sbxloop:run")})
        assert self.make(fresh).claim(self.make(fresh).poll()[0]) is True
        assert not any(
            p.endswith("sbxloop%3Adelivered") for m, p, _ in fresh.raw_calls if m == "DELETE"
        )
        stale = RecordingOps({"5": issue(5, "sbxloop:run", "sbxloop:delivered")})
        assert self.make(stale).claim(self.make(stale).poll()[0]) is True
        assert any(
            p.endswith("sbxloop%3Adelivered") for m, p, _ in stale.raw_calls if m == "DELETE"
        )

    def test_audit_claim_swaps_the_audit_label(self) -> None:
        ops = RecordingOps({"5": issue(5, "sbxloop:audit")})
        item = self.make(ops).poll()[0]
        assert self.make(ops).claim(item) is True
        deleted = [p for m, p, _ in ops.raw_calls if m == "DELETE"]
        assert any(p.endswith("sbxloop%3Aaudit") for p in deleted), deleted
        assert not any(p.endswith("sbxloop%3Arun") for p in deleted)

    def test_audit_success_closes_and_lists_filed_findings(self) -> None:
        """An audit is a chore whose output is the issues it filed: it closes
        on completion even in keep-open mode, and the comment names them."""
        ops = RecordingOps({"5": issue(5, "sbxloop:audit")})
        src = GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db", close_on_success=False)  # type: ignore[arg-type]
        item = src.poll()[0]
        src.report_success(item, report(filed=("gh:12", "gh:13")))
        assert any("Filed: #12, #13" in body for _, body in ops.comments)
        assert any(
            m == "PATCH" and b == {"state": "closed", "state_reason": "completed"}
            for m, _, b in ops.raw_calls
        )
        # a patch item in the same mode stays open (unchanged semantics)
        ops2 = RecordingOps({"4": issue(4, "sbxloop:run")})
        src2 = GitHubIssueSource(lambda: ops2, "o/r", LABELS, host="db", close_on_success=False)  # type: ignore[arg-type]
        src2.report_success(src2.poll()[0], report())
        assert not any(m == "PATCH" for m, _, _ in ops2.raw_calls)

    def test_the_review_charter_never_claims_gh_works(self) -> None:
        """The review sandbox holds no GitHub credential (#437): the charter
        must send the reviewer to git, not to `gh` subcommands that fail."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        src = GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db")  # type: ignore[arg-type]
        src.file_review(src.poll()[0], 9, "https://x/pull/9", "r1")
        body = ops.created_bodies[-1]
        assert "Charter" in body
        assert "gh pr diff" not in body and "gh pr view" not in body
        assert "git diff origin/" in body

    def test_pr_review_feedback_quotes_bodies_and_anchors(self) -> None:
        ops = RecordingOps()
        ops.review_rows = [
            {"user": {"login": "alice"}, "state": "CHANGES_REQUESTED", "body": "lock is missing"},
            {"user": {"login": "bob"}, "state": "APPROVED", "body": "lgtm"},
        ]
        ops.pr_comment_rows = [
            {"path": "a.py", "line": 9, "body": "off by one"},
            {"path": "", "line": None, "body": ""},  # empty body dropped
        ]
        src = GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db")  # type: ignore[arg-type]
        feedback = src.pr_review_feedback(9)
        assert "lock is missing" in feedback
        assert "`a.py:9`: off by one" in feedback
        assert "lgtm" not in feedback  # an approval is not an objection

    def test_pr_review_feedback_honours_latest_verdict_per_reviewer(self) -> None:
        """A reviewer whose change request was superseded by their own later
        approval no longer objects — quoting the stale body would send the
        fix round after feedback that was already withdrawn."""
        ops = RecordingOps()
        ops.review_rows = [
            {"user": {"login": "alice"}, "state": "CHANGES_REQUESTED", "body": "fix the race"},
            {"user": {"login": "alice"}, "state": "APPROVED", "body": "fixed, thanks"},
        ]
        src = GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db")  # type: ignore[arg-type]
        assert "fix the race" not in src.pr_review_feedback(9)

    def test_review_and_tool_filing_shapes(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        src = GitHubIssueSource(  # type: ignore[arg-type]
            lambda: ops, "o/r", LABELS, host="db", tool_repo="brettbergin/sbxloop"
        )
        item = src.poll()[0]
        ref = src.file_review(item, 9, "https://x/pull/9", "r1")
        assert ref.startswith("gh:")
        title, labels = ops.created[-1]
        assert title.startswith("review: PR #9 — Issue 4 (run r1)") and labels == ["sbxloop:audit"]
        up = src.file_tool_backlog("Planner nests sh -c", "evidence", "r1")
        assert up is not None and up.startswith("brettbergin/sbxloop#")
        # the upstream issue was created in the TOOL repo, not the project's
        assert ops.created_in[-1] == "brettbergin/sbxloop" and ops.created_in[-2] == "o/r"
        none = GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db")  # type: ignore[arg-type]
        assert none.file_tool_backlog("x", "y", "r1") is None
        # closing comment names upstream refs and noted-only titles
        src.report_success(
            item,
            report(tool_filed=("brettbergin/sbxloop#12",), tool_noted=("Prompt says X",)),
        )
        joined = "\n".join(b for _, b in ops.comments)
        assert "Filed upstream (about sbxloop itself): brettbergin/sbxloop#12" in joined
        assert "noted but not filed here" in joined and "Prompt says X" in joined

    def test_audit_with_no_findings_says_so(self) -> None:
        ops = RecordingOps({"5": issue(5, "sbxloop:audit")})
        item = self.make(ops).poll()[0]
        self.make(ops).report_success(item, report())
        assert any("filed no findings" in body for _, body in ops.comments)

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
            # no stale-delivered sweep: the re-GET showed no such label
        ]

    def test_claim_failure_after_adding_in_progress_rolls_it_back(self) -> None:
        """If removing the trigger fails, in-progress must come back off so
        the issue is exactly as found (review: otherwise the item is lost —
        polling only looks for the trigger)."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        ops.fail_on = {"DELETE"}  # trigger removal fails
        item = self.make(ops).poll()[0]
        # DELETE fails both for the trigger removal AND the rollback attempt;
        # rollback is best-effort. Verify the rollback was *attempted* by
        # allowing DELETE for the rollback path only.
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
        ops.fail_on = set()
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
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
        assert self.make(stale).claim(item) is False
        assert all(m == "GET" for m, _, _ in stale.raw_calls)  # no mutations
        closed = RecordingOps({"4": issue(4, "sbxloop:run", state="closed")})
        assert self.make(closed).claim(item) is False

    def test_success_leaves_a_patch_issue_open_with_delivered_label(self) -> None:
        """A delivered PR is not a resolution: the issue settles on merge
        (report_merged), so success only marks it delivered — whatever the
        deprecated close_on_success says."""
        for closing in (True, False):
            ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
            item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
            src = GitHubIssueSource(
                lambda ops=ops: ops,  # type: ignore[misc]
                "o/r",
                LABELS,
                host="db",
                close_on_success=closing,
            )
            src.report_success(item, report())
            assert not any(m == "PATCH" for m, _, _ in ops.raw_calls), closing
            assert (
                "DELETE",
                "/repos/o/r/issues/4/labels/sbxloop%3Ain-progress",
                None,
            ) in ops.raw_calls
            assert (
                "POST",
                "/repos/o/r/issues/4/labels",
                {"labels": ["sbxloop:delivered"]},
            ) in ops.raw_calls
            body = ops.comments[-1][1]
            assert "pull/9" in body and "closed when the PR merges" in body

    def test_success_closes_an_audit_with_completed_label(self) -> None:
        """An audit has no PR whose merge could settle it: it closes on
        completion, wearing the completed label as the sbxloop mark."""
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x", kind="audit")
        self.make(ops).report_success(item, report())
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

    def test_report_merged_labels_then_closes(self) -> None:
        """The merge settles the issue: completed label on, lifecycle labels
        off, closed as completed — labels before the close, so a mid-way
        failure leaves an open, correctly labelled issue."""
        ops = RecordingOps({"4": issue(4, "sbxloop:delivered")})
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
        assert self.make(ops).report_merged(item, 9, "https://x/pull/9") is True
        assert any("pull/9" in body and "merged" in body for _, body in ops.comments)
        writes = [(m, p) for m, p, _ in ops.raw_calls if m in {"POST", "PATCH", "DELETE"}]
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Adelivered") in writes
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

    def test_report_merged_failure_returns_false_for_a_retry(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:delivered")})
        ops.fail_on = {"PATCH"}
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
        assert self.make(ops).report_merged(item, 9, "u") is False

    def test_report_pr_closed_marks_failed_and_leaves_the_issue_open(self) -> None:
        """A human closing the PR unmerged is a rejection: failed label on,
        issue left open for the human to re-trigger or close."""
        ops = RecordingOps({"4": issue(4, "sbxloop:delivered")})
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
        assert self.make(ops).report_pr_closed(item, 9, "https://x/pull/9") is True
        assert not any(m == "PATCH" for m, _, _ in ops.raw_calls)
        assert (
            "POST",
            "/repos/o/r/issues/4/labels",
            {"labels": ["sbxloop:failed"]},
        ) in ops.raw_calls
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Adelivered", None) in ops.raw_calls
        assert any("closed without being merged" in body for _, body in ops.comments)

    def test_pr_merge_state_folds_the_pr_payload(self) -> None:
        ops = RecordingOps()
        ops.prs[9] = {"state": "closed", "merged": True}
        assert self.make(ops).pr_merge_state(9) == (True, "closed")
        ops.prs[9] = {"state": "open", "merged": False}
        assert self.make(ops).pr_merge_state(9) == (False, "open")

    def test_pr_state_short_circuits_a_merged_or_closed_pr(self) -> None:
        """A merged or closed PR is past checks and review: one read, not
        three, and the snapshot carries the fate the ladder acts on."""
        ops = RecordingOps()
        ops.prs[9] = {"state": "closed", "merged": True, "head": {"sha": "abc"}}
        snap = self.make(ops).pr_state(9)
        assert snap.merged and snap.state == "closed"
        assert ops.pr_reads == ["get"]
        ops.pr_reads.clear()
        ops.prs[9] = {"state": "open", "merged": False, "head": {"sha": "abc"}}
        snap = self.make(ops).pr_state(9)
        assert not snap.merged and snap.state == "open"
        assert ops.pr_reads == ["get", "checks", "reviews"]

    def test_pr_state_surfaces_the_pr_labels_from_the_same_read(self) -> None:
        """The payload the head sha comes from already carries the labels, so
        the hands-off check costs no extra API call."""
        ops = RecordingOps()
        ops.prs[9] = {
            "state": "open",
            "merged": False,
            "head": {"sha": "abc"},
            "labels": [{"name": "Bug"}, {"name": "sbxloop:hands-off"}, {"name": ""}, "junk"],
        }
        snap = self.make(ops).pr_state(9)
        assert snap.labels == ("bug", "sbxloop:hands-off")
        assert ops.pr_reads == ["get", "checks", "reviews"]

    def test_pr_state_without_labels_reports_none(self) -> None:
        ops = RecordingOps()
        ops.prs[9] = {"state": "open", "merged": False, "head": {"sha": "abc"}}
        assert self.make(ops).pr_state(9).labels == ()

    def test_requeued_strips_the_completed_label_too(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:completed")})
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
        self.make(ops).report_requeued(item, "b")
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Acompleted", None) in ops.raw_calls

    def test_claim_clears_stale_delivered_label_in_either_mode(self) -> None:
        """A rejected PR's issue gets re-triggered; the delivered label from
        the previous run must not survive into the new one. The label was
        written by whatever mode was configured *then*, so a daemon that has
        since flipped back to closing must clear it too."""
        for closing in (False, True):
            ops = RecordingOps({"4": issue(4, "sbxloop:run", "sbxloop:delivered")})
            src = GitHubIssueSource(
                lambda ops=ops: ops,  # type: ignore[misc]
                "o/r",
                LABELS,
                host="db",
                close_on_success=closing,
            )
            item = src.poll()[0]
            assert src.claim(item) is True
            deletes = [p for m, p, _ in ops.raw_calls if m == "DELETE"]
            assert "/repos/o/r/issues/4/labels/sbxloop%3Adelivered" in deletes, closing

    def test_delivery_failure_leaves_open_with_failed_label(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
        self.make(ops).report_delivery_failed(item, report(delivery=None, delivery_error="409"))
        assert not any(m == "PATCH" for m, _, _ in ops.raw_calls)
        assert (
            "POST",
            "/repos/o/r/issues/4/labels",
            {"labels": ["sbxloop:failed"]},
        ) in ops.raw_calls
        assert any("could not be delivered" in body for _, body in ops.comments)

    def test_abandoned_adds_failed_label_with_retrigger_hint(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x", claimed=True)
        self.make(ops).report_abandoned(item, "budget exhausted")
        assert (
            "POST",
            "/repos/o/r/issues/4/labels",
            {"labels": ["sbxloop:failed"]},
        ) in ops.raw_calls
        assert any("re-adding `sbxloop:run`" in body for _, body in ops.comments)
        # Claimed: the trigger label already went with the claim; not touched.
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Arun", None) not in ops.raw_calls

    def test_abandoned_while_unclaimed_drops_the_trigger_label(self) -> None:
        """#229: an operator abandons an item that was never claimed (still
        queued). The trigger label is what is on the issue; left there the
        issue reads as work to do and "re-add the trigger" is a no-op."""
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x", claimed=False)
        self.make(ops).report_abandoned(item, "abandoned by operator")
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
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
        cancelled = report(
            state="cancelled", delivery=None, tracking_issue=None, cancelled_by="Discord user `b`"
        )
        self.make(ops).report_cancelled(item, cancelled)
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Ain-progress", None) in ops.raw_calls
        assert not any(m == "POST" for m, _, _ in ops.raw_calls)  # no failed label
        body = ops.comments[-1][1]
        assert "cancelled by Discord user `b`" in body
        assert "`sbxloop resume r1`" in body and "!sbx retry gh:4" in body

    def test_cancelled_with_requeue_keeps_in_progress(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
        self.make(ops).report_cancelled(item, report(state="cancelled", requeued=True))
        assert not any(m == "DELETE" for m, _, _ in ops.raw_calls)
        assert "Re-queued" in ops.comments[-1][1]

    def test_requeued_reclaims_with_in_progress_and_drops_failed(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:failed")})
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
        self.make(ops).report_requeued(item, "Discord user `b`")
        assert (
            "POST",
            "/repos/o/r/issues/4/labels",
            {"labels": ["sbxloop:in-progress"]},
        ) in ops.raw_calls
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Afailed", None) in ops.raw_calls
        assert "Re-queued by Discord user `b`" in ops.comments[-1][1]

    def test_requeued_clears_delivered_label_before_claiming(self) -> None:
        """An operator re-queue of a done item must not leave the issue
        wearing in-progress and delivered together — in either mode, since
        the label may have been written under the other one — and the stale
        labels go first so a swallowed failure cannot leave both behind."""
        for closing in (False, True):
            ops = RecordingOps({"4": issue(4, "sbxloop:delivered")})
            item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
            src = GitHubIssueSource(
                lambda ops=ops: ops,  # type: ignore[misc]
                "o/r",
                LABELS,
                host="db",
                close_on_success=closing,
            )
            src.report_requeued(item, "b")
            paths = [(m, p) for m, p, _ in ops.raw_calls if m in {"DELETE", "POST"}]
            delivered = paths.index(("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Adelivered"))
            in_progress = paths.index(("POST", "/repos/o/r/issues/4/labels"))
            assert delivered < in_progress, closing
        # a failing label removal must not leave in-progress added on top
        ops = RecordingOps({"4": issue(4, "sbxloop:delivered")})
        ops.fail_on = {"DELETE"}
        src.report_requeued(item, "b")
        assert not any(m == "POST" for m, _, _ in ops.raw_calls)

    def test_reporting_failures_are_swallowed(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        ops.fail_on = {"COMMENT", "PATCH", "DELETE", "POST"}
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
        src = self.make(ops)
        src.report_started(item, "r1")  # must not raise
        src.report_success(item, report())
        src.report_retry(item, "err", 1)
        src.report_abandoned(item, "err")
        src.report_cancelled(item, report(state="cancelled"))
        src.report_requeued(item, "b")

    def test_file_backlog_label_per_trigger(self) -> None:
        ops = RecordingOps()
        src = self.make(ops)
        assert src.file_backlog("Later", "detail", "r1", trigger=False) == "gh:77"
        assert ops.created[-1] == ("Later", ["sbxloop:backlog"])
        src.file_backlog("Now", "detail", "r1", trigger=True)
        assert ops.created[-1] == ("Now", ["sbxloop:run"])


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
        assert "'item': 'gh:4'" in claimed and "'duration_s'" in claimed

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
        assert "'item': 'gh:4'" in declined and "trigger label gone" in declined


class TestDaemonGithubInstance:
    def test_sandbox_name_is_per_state_dir(self, tmp_path: Path) -> None:
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

    def test_reprovision_is_rate_limited(self, tmp_path: Path) -> None:
        """A GitHub outage used to cost one microVM rebuild per failing
        call; now at most one per REPROVISION_MIN_INTERVAL_S (#254)."""
        from sbxloop.config import Config
        from sbxloop.daemon.github import REPROVISION_MIN_INTERVAL_S, DaemonGithub
        from sbxloop.events import EventBus

        config = Config.model_validate({"state_dir": str(tmp_path / "state")})
        now = [1000.0]
        gh = DaemonGithub(
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

        gh._provision = provision  # type: ignore[method-assign]
        gh.ops()
        assert provisions == 1
        assert gh.note_failure(GithubOpsError("HTTP 502")) is True
        gh.ops()
        assert provisions == 2
        now[0] += 10
        assert gh.note_failure(GithubOpsError("HTTP 502")) is False
        gh.ops()
        assert provisions == 2  # still the same sandbox
        now[0] += REPROVISION_MIN_INTERVAL_S
        assert gh.note_failure(GithubOpsError("HTTP 502")) is True
        gh.ops()
        assert provisions == 3

    def test_call_retries_once_after_reprovision_and_raises_when_throttled(
        self, tmp_path: Path
    ) -> None:
        from sbxloop.config import Config
        from sbxloop.daemon.github import DaemonGithub
        from sbxloop.events import EventBus

        config = Config.model_validate({"state_dir": str(tmp_path / "state")})
        gh = DaemonGithub(
            config, sbx=object(), bus=EventBus(), worker_python="python3", clock=lambda: 0.0
        )  # type: ignore[arg-type]
        gh._provision = lambda: object()  # type: ignore[method-assign, assignment]
        calls = 0

        def flaky(_ops: object) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise GithubOpsError("boom")
            return "ok"

        assert gh.call(flaky) == "ok" and calls == 2
        with pytest.raises(GithubOpsError):
            gh.call(lambda _ops: (_ for _ in ()).throw(GithubOpsError("again")))


class TestDaemonGithubProvisioning:
    def test_provision_error_is_wrapped_as_daemon_error(self, tmp_path: Path) -> None:
        """ensure_github_only can raise ProvisionError (not just SbxError);
        both must surface as one DaemonError (review)."""
        from sbxloop.config import Config
        from sbxloop.daemon.github import DaemonGithub
        from sbxloop.errors import DaemonError, ProvisionError
        from sbxloop.events import EventBus

        config = Config.model_validate({"state_dir": str(tmp_path / "state")})
        gh = DaemonGithub(config, sbx=object(), bus=EventBus(), worker_python="python3")  # type: ignore[arg-type]

        class Boom:
            def ensure_github_only(self, *a: object, **k: object) -> object:
                raise ProvisionError("GH_TOKEN is not set")

        gh.provisioner = Boom()  # type: ignore[assignment]
        with pytest.raises(DaemonError, match="GH_TOKEN"):
            gh.ops()
