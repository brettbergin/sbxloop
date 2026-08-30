"""Follow-ups from a landed run (#517): the pure half — collect, dedup,
render — and the prompt/schema contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sbxloop.engine.followups import (
    checklist_comment,
    collect_followups,
    followup_key,
    followup_marker,
    issue_body,
    marker_key,
)
from sbxloop.engine.review import Followup, ReviewFinding, ReviewRound, ReviewVerdict, review_body


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
        assert (
            "It is **not** queued for the loop: add the trigger label if you want it run." in body
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
