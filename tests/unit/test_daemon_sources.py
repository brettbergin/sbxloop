"""Work sources against tmp dirs and a recording GithubOps stub."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.daemon.sources import (
    GitHubIssueSource,
    GitHubLabels,
    InboxSource,
    parse_markdown_item,
)
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import IssueRef

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

    def test_file_backlog_triage_vs_trigger(self, tmp_path: Path) -> None:
        src, root = self.make(tmp_path)
        ref = src.file_backlog("Add caching", "it is slow", "r7", trigger=False)
        assert ref.startswith("inbox:add-caching-")
        (path,) = list((root / "triage").glob("add-caching-*.md"))
        assert "# Add caching" in path.read_text() and "run r7" in path.read_text()
        src.file_backlog("Add caching", "it is slow", "r7", trigger=True)
        assert list((root / "pending").glob("add-caching-*.md"))


class RecordingOps:
    """GithubOps stand-in for the issue source: scripted GET, recorded writes."""

    def __init__(self, issues: dict[str, dict[str, Any]] | None = None) -> None:
        self.issues = issues or {}
        self.searches: list[str] = []
        self.raw_calls: list[tuple[str, str, Any]] = []
        self.comments: list[tuple[int, str]] = []
        self.created: list[tuple[str, list[str] | None]] = []
        self.fail_on: set[str] = set()

    def search_issues(self, query: str, per_page: int = 30) -> list[dict[str, Any]]:
        self.searches.append(query)
        return list(self.issues.values())

    def raw(self, method: str, path: str, body: Any = None) -> Any:
        if method in self.fail_on:
            raise GithubOpsError(f"{method} {path} -> HTTP 500: boom")
        self.raw_calls.append((method, path, body))
        if method == "GET":
            number = path.rsplit("/", 1)[-1]
            return self.issues.get(number, {"state": "closed", "labels": []})
        return {}

    def issue_comment(self, repo: str, number: int, body: str) -> str:
        if "COMMENT" in self.fail_on:
            raise GithubOpsError("comment -> HTTP 502")
        self.comments.append((number, body))
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


class TestGitHubSource:
    def make(self, ops: RecordingOps) -> GitHubIssueSource:
        return GitHubIssueSource(lambda: ops, "o/r", LABELS, host="db")  # type: ignore[arg-type]

    def test_poll_uses_trigger_label_query(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        items = self.make(ops).poll()
        assert ops.searches == ['repo:o/r is:issue is:open label:"sbxloop:run"']
        assert [i.item_id for i in items] == ["gh:4"]
        assert items[0].url == "https://x/issues/4" and items[0].body == "please do it"

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
        # no claim comment was posted for a failed claim
        assert not any("claimed" in body for _, body in ops.comments)

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

    def test_claim_comment_failure_does_not_unclaim(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:run")})
        ops.fail_on = {"COMMENT"}
        item = self.make(ops).poll()[0]
        assert self.make(ops).claim(item) is True  # labels swapped; comment is cosmetic

    def test_claim_refuses_when_label_gone_or_issue_closed(self) -> None:
        stale = RecordingOps({"4": issue(4)})  # trigger label already removed
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
        assert self.make(stale).claim(item) is False
        assert all(m == "GET" for m, _, _ in stale.raw_calls)  # no mutations
        closed = RecordingOps({"4": issue(4, "sbxloop:run", state="closed")})
        assert self.make(closed).claim(item) is False

    def test_success_closes_with_state_reason(self) -> None:
        ops = RecordingOps({"4": issue(4, "sbxloop:in-progress")})
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
        self.make(ops).report_success(item, report())
        assert any("pull/9" in body for _, body in ops.comments)
        assert ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Ain-progress", None) in ops.raw_calls
        assert (
            "PATCH",
            "/repos/o/r/issues/4",
            {"state": "closed", "state_reason": "completed"},
        ) in ops.raw_calls

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
        item = WorkItem(item_id="gh:4", source="github", source_key="4", title="x")
        self.make(ops).report_abandoned(item, "budget exhausted")
        assert (
            "POST",
            "/repos/o/r/issues/4/labels",
            {"labels": ["sbxloop:failed"]},
        ) in ops.raw_calls
        assert any("re-adding `sbxloop:run`" in body for _, body in ops.comments)

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
        assert "`sbxloop resume r1`" in body and "!sbx requeue gh:4" in body

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
