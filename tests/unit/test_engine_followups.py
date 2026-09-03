"""Follow-ups from a landed run (#517): the pure half — collect, dedup,
render — and the prompt/schema contract."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from pydantic import ValidationError

from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.followups import (
    checklist_comment,
    collect_followups,
    followup_key,
    followup_marker,
    issue_body,
    marker_key,
)
from sbxloop.engine.review import Followup, ReviewFinding, ReviewRound, ReviewVerdict, review_body
from sbxloop.errors import GithubOpsError
from sbxloop.events import HostEventTypes
from tests.conftest import FakeSbx
from tests.fakes.fake_github import FakeGithub
from tests.fakes.github_errors import github_error
from tests.unit.test_engine import (
    FILES_BUILD,
    FINDING,
    Harness,
    new_run_id_for,
    review,
    task,
    taskgraph,
)


def finding(path: str = "src/app.py", line: int | None = 12, **kw: object) -> ReviewFinding:
    fields: dict[str, object] = {
        "path": path,
        "line": line,
        "body": "the lock is released before the write",
        "severity": "major",
        "repro": "two writers; second write lost",
    }
    fields.update(kw)
    return ReviewFinding.model_validate(fields)


def verdict(*followups: Followup, findings: list[ReviewFinding] | None = None) -> ReviewVerdict:
    return ReviewVerdict(
        verdict="request_changes" if findings else "approve",
        summary="looked",
        findings=findings or [],
        followups=list(followups),
    )


class TestSchema:
    def test_followup_needs_a_title_and_renders(self) -> None:
        with pytest.raises(ValidationError, match="needs a `title`"):
            Followup(title="   ")
        f = Followup(
            title="doctor boots one microVM per repo",
            body="one per\ncredential would do",
            path="src/cli/doctor.py",
            line=40,
        )
        assert f.anchor == "src/cli/doctor.py:40"
        assert f.render() == (
            "- **doctor boots one microVM per repo** (`src/cli/doctor.py:40`) — "
            "one per credential would do"
        )
        assert Followup(title="t").render() == "- **t**" and Followup(title="t").anchor == ""

    def test_verdict_parses_and_renders_followups(self) -> None:
        v = ReviewVerdict.model_validate(
            {
                "verdict": "approve",
                "summary": "clean",
                "followups": [
                    {"title": "unread config key", "body": "labels is parsed, read nowhere"}
                ],
            }
        )
        assert [f.title for f in v.followups] == ["unread config key"]
        body = review_body(v, run_id="r1", round=1)
        assert "Follow-ups — real, but out of scope for this pull request" in body
        assert "- **unread config key** — labels is parsed, read nowhere" in body
        assert "Follow-ups" not in review_body(
            ReviewVerdict(verdict="approve", summary="x"), run_id="r1", round=1
        )
        assert "Follow-ups" in review_body(v, run_id="r1", round=1, anchored=False)
        assert "Follow-ups" in review_body(v, run_id="r1", round=1, in_body=[])


class TestCollect:
    def test_dedups_across_rounds_by_normalised_title(self) -> None:
        a = Followup(title="Doctor boots one microVM per repo.", body="round one")
        a2 = Followup(title="doctor boots one micro-VM per repo", body="round two restates it")
        b = Followup(title="permanently-404 repo warns every poll", path="src/daemon/sources.py")
        rounds = [
            ReviewRound(1, verdict(a, findings=[finding()]), "- addressed: src/app.py:12 — done"),
            ReviewRound(2, verdict(a2, b), ""),
        ]
        got = collect_followups(rounds)
        assert [(c.followup.title, c.round, c.source) for c in got] == [
            ("Doctor boots one microVM per repo.", 1, "review"),
            ("permanently-404 repo warns every poll", 2, "review"),
        ]
        assert followup_key(a.title) == followup_key(a2.title) == got[0].key

    def test_deferred_findings_become_followups(self) -> None:
        minor = finding(
            path="src/config.py",
            line=3,
            severity="minor",
            body="RepoConfig.labels is parsed and documented but read nowhere.",
            repro="grep labels finds no reader",
        )
        rounds = [
            ReviewRound(
                1,
                verdict(findings=[finding(), minor]),
                "- addressed: src/app.py:12 — lock\n"
                "- deferred: src/config.py:3 — not this PR's job",
            ),
        ]
        (cand,) = collect_followups(rounds)
        assert cand.source == "deferred" and cand.round == 1
        assert cand.followup.title == "RepoConfig.labels is parsed and documented but read nowhere"
        assert cand.followup.anchor == "src/config.py:3"
        assert "deferred by the fix round: not this PR's job" in cand.followup.body
        assert "Repro: grep labels finds no reader" in cand.followup.body

    def test_a_reviewer_followup_with_the_same_title_wins_over_the_deferral(self) -> None:
        minor = finding(path="src/config.py", line=3, severity="minor", body="unread key")
        rounds = [
            ReviewRound(
                1,
                verdict(
                    Followup(title="Unread key", body="from the reviewer"),
                    findings=[finding(), minor],
                ),
                "- addressed: src/app.py:12 — lock\n- deferred: src/config.py:3 — later",
            ),
        ]
        (cand,) = collect_followups(rounds)
        assert cand.source == "review" and cand.followup.body == "from the reviewer"

    def test_nothing_from_rounds_without_followups_or_deferrals(self) -> None:
        rounds = [ReviewRound(1, verdict(findings=[finding()]), "- refuted: src/app.py:12 — fine")]
        assert collect_followups(rounds) == [] and collect_followups([]) == []


class TestRendering:
    def test_issue_body_cites_origin_and_carries_the_marker(self) -> None:
        (cand,) = collect_followups(
            [ReviewRound(2, verdict(Followup(title="T", body="B", path="a.py", line=1)), "")]
        )
        body = issue_body(
            cand, run_id="r1", repo="o/r", pr_number=7, pr_url="https://x/pull/7", closes=511
        )
        assert body.startswith("B\n\nWhere: `a.py:1`\n\n")
        assert (
            "Out of scope for [PR #7](https://x/pull/7) (issue #511), noted by the review "
            "in round 2; run `r1` on `o/r`." in body
        )
        # No daemon dispatched this run: nothing polls the repository, so
        # the body must not point at a trigger label (#631).
        assert "It is **not** queued for the loop." in body and "label" not in body
        queued = issue_body(
            cand,
            run_id="r1",
            repo="o/r",
            pr_number=7,
            pr_url="https://x/pull/7",
            closes=511,
            trigger_label="loop:go",
        )
        assert (
            "It is **not** queued for the loop: add the `loop:go` label if you want it run."
            in queued
        )
        assert body.endswith(followup_marker("r1", cand.key))
        assert marker_key(body) == ("r1", cand.key)
        assert marker_key("no marker here") is None

    def test_issue_body_for_a_deferral(self) -> None:
        minor = finding(path="src/config.py", line=3, severity="minor", body="unread key")
        (cand,) = collect_followups(
            [
                ReviewRound(
                    1,
                    verdict(findings=[finding(), minor]),
                    "- addressed: src/app.py:12 — ok\n- deferred: src/config.py:3 — later",
                )
            ]
        )
        body = issue_body(cand, run_id="r1", repo="o/r", pr_number=7, pr_url="", closes=None)
        assert (
            "Out of scope for PR #7, a review finding of round 1 the fix round deferred; "
            "run `r1` on `o/r`." in body
        )

    def test_checklist_comment_points_at_filed_issues_or_lists_them(self) -> None:
        cands = collect_followups(
            [ReviewRound(1, verdict(Followup(title="A", body="a"), Followup(title="B")), "")]
        )
        filed = checklist_comment(
            cands, run_id="r1", filed=[("A", "https://x/issues/1"), ("B", "https://x/issues/2")]
        )
        assert "filed as issues (not queued for the loop)" in filed
        assert "- [A](https://x/issues/1)\n- [B](https://x/issues/2)" in filed
        listed = checklist_comment(cands, run_id="r1")
        assert 'Not filed as issues (`[landing] followups = "comment"`)' in listed
        assert "- [ ] **A** — a\n- [ ] **B**" in listed
        assert listed.endswith("<!-- sbxloop-followups run=r1 -->")
        downgraded = checklist_comment(cands, run_id="r1", reason="Issues are disabled here")
        assert "Not filed as issues (Issues are disabled here)" in downgraded


class FakeOps:
    """Just enough of GithubOps to watch the label calls (#556): the probe
    goes through ``label_lookup`` (a 404 already turned into None by the
    worker op), the creation through ``raw``."""

    def __init__(
        self,
        *,
        get: dict[str, Any] | None = None,
        get_error: Exception | None = None,
        post_error: Exception | None = None,
    ) -> None:
        self._get = get
        self._get_error = get_error
        self._post_error = post_error
        self.calls: list[tuple[str, str]] = []

    def label_lookup(self, repo: str, name: str) -> dict[str, Any] | None:
        self.calls.append(("LOOKUP", f"/repos/{repo}/labels/{quote(name, safe='')}"))
        if self._get_error is not None:
            raise self._get_error
        return self._get

    def raw(self, method: str, path: str, body: object = None) -> object:
        self.calls.append((method, path))
        if self._post_error is not None:
            raise self._post_error
        return {"name": "x"}


class TestEnsureLabel:
    def test_existing_label_is_silent_success_without_a_post(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ops = FakeOps(get={"name": "sbxloop follow-up"})
        with caplog.at_level(logging.DEBUG):
            LoopEngine._ensure_label(ops, "o/r", "sbxloop follow-up")  # type: ignore[arg-type]
        assert ops.calls == [("LOOKUP", "/repos/o/r/labels/sbxloop%20follow-up")]
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_missing_label_is_created(self) -> None:
        ops = FakeOps(get=None)
        LoopEngine._ensure_label(ops, "o/r", "followup")  # type: ignore[arg-type]
        assert ops.calls == [
            ("LOOKUP", "/repos/o/r/labels/followup"),
            ("POST", "/repos/o/r/labels"),
        ]

    def test_lookup_failure_that_is_not_a_miss_warns_once_and_skips_the_post(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 403 (token without repo scope) or a 5xx is not "absent": the
        POST behind it could only fail too, so it is never made."""
        for status in (403, 500):
            ops = FakeOps(get_error=GithubOpsError("nope", http_status=status))
            caplog.clear()
            with caplog.at_level(logging.DEBUG):
                LoopEngine._ensure_label(ops, "o/r", "followup")  # type: ignore[arg-type]
            assert ops.calls == [("LOOKUP", "/repos/o/r/labels/followup")]
            assert [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_already_exists_on_create_is_success(self, caplog: pytest.LogCaptureFixture) -> None:
        ops = FakeOps(get=None, post_error=GithubOpsError("nope", http_status=422))
        with caplog.at_level(logging.DEBUG):
            LoopEngine._ensure_label(ops, "o/r", "followup")  # type: ignore[arg-type]
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_unexpected_failure_warns_but_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ops = FakeOps(get=None, post_error=GithubOpsError("boom", http_status=500))
        with caplog.at_level(logging.DEBUG):
            LoopEngine._ensure_label(ops, "o/r", "followup")  # type: ignore[arg-type]
        assert any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


FOLLOWUP_A = {
    "title": "doctor boots one microVM per configured repo",
    "body": "one per credential would do",
    "path": "src/cli/doctor.py",
}
FOLLOWUP_B = {"title": "a permanently-404 repo warns every poll forever"}


def followup_script() -> list[dict[str, Any]]:
    """A run whose review leaves two notes out of scope and defers a nit."""
    minor = {
        "path": "hello.txt",
        "line": 2,
        "body": "the greeting is not documented",
        "severity": "minor",
    }
    round_one = review("request_changes", "one problem, one nit", FINDING, minor)
    round_one["json"]["followups"] = [FOLLOWUP_A]
    fix = {"text": "Fixed.\n\naddressed: hello.txt:1 — hello\ndeferred: hello.txt:2 — docs later"}
    round_two = review("approve", "fixed; the nit is deferred")
    round_two["json"]["followups"] = [FOLLOWUP_B]
    return [taskgraph(task("t1")), FILES_BUILD, round_one, fix, round_two]


class RaceyLabelGithub(FakeGithub):
    """The pre-check says the label is missing, creation says it exists."""

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if method == "GET" and "/labels/" in path:
            self.raw_calls.append((method, path, body))
            raise github_error("label_missing_404")
        return super().raw(method, path, body)


class TestFilingWithExistingLabels:
    """#556: a run files its out-of-scope follow-ups whatever labels the
    repository already carries."""

    def _run(self, harness: Harness, fake: FakeGithub) -> Any:
        harness.script(followup_script())
        return harness.pipeline(fake).start("ship hello")

    @staticmethod
    def _label_posts(fake: FakeGithub) -> list[tuple[str, str, Any]]:
        return [c for c in fake.raw_calls if c[0] == "POST" and c[1].endswith("/labels")]

    def test_files_followups_when_every_label_already_exists(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake = FakeGithub()
        fake.labels_existing.add("sbxloop:follow-up")
        with caplog.at_level(logging.DEBUG):
            result = self._run(harness, fake)
        assert result.state == "merged"
        assert len(fake.issues_created) == 3
        for _, _, labels in fake.issues_created:
            assert labels == ["sbxloop:follow-up"]
        assert self._label_posts(fake) == []
        # Not merely "nothing raised": the run's chronology carries no
        # failed worker job either (#559).
        fake.assert_no_failed_jobs()
        assert [
            r for r in caplog.records if r.levelno >= logging.WARNING and "label" in r.getMessage()
        ] == []

    def test_absent_label_is_created_once(self, harness: Harness) -> None:
        fake = FakeGithub()
        result = self._run(harness, fake)
        assert result.state == "merged"
        assert fake.labels_created == ["sbxloop:follow-up"]
        assert len(self._label_posts(fake)) == 1
        # The lookup missed, but a resolved miss is data: no failed job.
        assert fake.failed_jobs == []
        assert len(fake.issues_created) == 3
        for _, _, labels in fake.issues_created:
            assert labels == ["sbxloop:follow-up"]

    def test_a_race_between_the_check_and_the_create_is_not_an_error(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake = RaceyLabelGithub()
        fake.labels_existing.add("sbxloop:follow-up")
        with caplog.at_level(logging.DEBUG):
            result = self._run(harness, fake)
        assert result.state == "merged"
        assert len(self._label_posts(fake)) == 1
        assert len(fake.issues_created) == 3
        assert [
            r for r in caplog.records if r.levelno >= logging.WARNING and "label" in r.getMessage()
        ] == []


class TestIssuesDisabled:
    """#631: a repository with Issues turned off cannot take follow-up
    issues — they land as the PR checklist instead, and the run says so."""

    def _run(self, harness: Harness, fake: FakeGithub) -> Any:
        harness.script(followup_script())
        return harness.pipeline(fake).start("ship hello")

    @staticmethod
    def _followup_events(harness: Harness) -> list[Any]:
        return [e for e in harness.events if e.type == HostEventTypes.RUN_FOLLOWUPS]

    def test_has_issues_false_downgrades_to_the_pr_comment(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.has_issues = False
        result = self._run(harness, fake)
        assert result.state == "merged"
        assert fake.issues_created == []
        # Nothing was even attempted: no label ensure, no issue list.
        assert not any(path.endswith("/labels") for _m, path, _b in fake.raw_calls)
        (listed,) = [c for c in fake.issue_comments if c.startswith("## Follow-ups")]
        assert "Not filed as issues (Issues are disabled on this repository)" in listed
        assert "- [ ] **the greeting is not documented**" in listed
        (event,) = self._followup_events(harness)
        assert event.data["mode"] == "comment"
        assert event.data["downgraded_from"] == "issues"
        assert event.data["reason"] == "issues_disabled"
        assert len(event.data["listed"]) == 3

    def test_a_silent_payload_downgrades_on_the_410(self, harness: Harness) -> None:
        """The probe did not say (no ``has_issues`` key): the first filing's
        410 Gone decides, and the checklist still lands."""

        class SilentPayload(FakeGithub):
            def repo_lookup(self, repo: str) -> dict[str, Any] | None:
                payload = super().repo_lookup(repo)
                assert payload is not None
                payload.pop("has_issues", None)
                return payload

        fake = SilentPayload()
        fake.has_issues = False
        result = self._run(harness, fake)
        assert result.state == "merged"
        assert fake.issues_created == []
        (listed,) = [c for c in fake.issue_comments if c.startswith("## Follow-ups")]
        assert "Issues are disabled on this repository" in listed
        (event,) = self._followup_events(harness)
        assert event.data["mode"] == "comment" and event.data["reason"] == "issues_disabled"
        # The 410 is a downgrade, not a failed filing.
        assert fake.failed_jobs == []

    def test_issues_enabled_files_as_before(self, harness: Harness) -> None:
        fake = FakeGithub()
        result = self._run(harness, fake)
        assert result.state == "merged"
        assert len(fake.issues_created) == 3
        (event,) = self._followup_events(harness)
        assert event.data["mode"] == "issues" and "downgraded_from" not in event.data

    def test_a_labelled_pull_request_does_not_count_as_filed(self, harness: Harness) -> None:
        """The issues list includes pull requests; one carrying this run's
        marker (a PR body that quoted a follow-up) must not suppress the
        issue (#631)."""
        fake = FakeGithub()
        harness.script(followup_script())
        engine = harness.pipeline(fake)
        run_id = new_run_id_for(engine)
        fake.existing_issues = [
            {
                "html_url": "https://x/pull/2",
                "pull_request": {"url": "https://api/pulls/2"},
                "body": "…\n" + followup_marker(run_id, followup_key(FOLLOWUP_B["title"])),
            },
            {
                "html_url": "https://x/issues/3",
                "body": "…\n" + followup_marker(run_id, followup_key(FOLLOWUP_A["title"])),
            },
        ]
        result = engine.start("ship hello", run_id=run_id)
        assert result.state == "merged"
        assert sorted(t for t, _, _ in fake.issues_created) == [
            FOLLOWUP_B["title"],
            "the greeting is not documented",
        ]

    def test_a_daemon_dispatched_run_names_its_trigger_label(self, harness: Harness) -> None:
        fake = FakeGithub()
        harness.script(followup_script())
        engine = harness.pipeline(fake)
        engine.trigger_label = "sbxloop:run"
        assert engine.start("ship hello").state == "merged"
        for _, body, _ in fake.issues_created:
            assert "add the `sbxloop:run` label if you want it run" in body

    def test_a_cli_run_omits_the_trigger_instruction(self, harness: Harness) -> None:
        fake = FakeGithub()
        result = self._run(harness, fake)
        assert result.state == "merged"
        for _, body, _ in fake.issues_created:
            assert "not** queued for the loop." in body and "trigger label" not in body
