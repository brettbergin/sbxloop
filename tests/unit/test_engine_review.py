"""The review verdict, its memory across rounds, and the fix round it seeds
(engine.review) — pure model/rendering tests, no engine."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sbxloop.engine.review import (
    FIX_TASK_PREFIX,
    FIX_TASK_TITLE,
    MAX_INLINE_COMMENTS,
    ReviewFinding,
    ReviewGuard,
    ReviewRound,
    ReviewVerdict,
    fix_brief,
    fix_task,
    is_fix_task,
    reconcile,
    reconcile_rounds,
    refuted_anchors,
    render_fix_history,
    render_review_history,
    review_body,
    split_test,
)
from sbxloop.gh.ops import FailedCheck, ReviewComment


def finding(
    path: str = "src/app.py",
    line: int | None = 12,
    body: str = "the lock is released before the write",
    severity: str = "major",
    repro: str = "two writers on one row; observed: the second write is lost; expected: kept",
) -> ReviewFinding:
    return ReviewFinding.model_validate(
        {"path": path, "line": line, "body": body, "severity": severity, "repro": repro}
    )


def approve(*findings: ReviewFinding, summary: str = "looked for the four; clean") -> ReviewVerdict:
    return ReviewVerdict(verdict="approve", summary=summary, findings=list(findings))


def request_changes(*findings: ReviewFinding, summary: str = "one real defect") -> ReviewVerdict:
    return ReviewVerdict(verdict="request_changes", summary=summary, findings=list(findings))


class TestReviewFinding:
    def test_anchor_and_render(self) -> None:
        f = finding(repro="")
        assert f.anchor == "src/app.py:12"
        assert f.blocking
        assert f.render() == "- `src/app.py:12` [major] the lock is released before the write"
        assert f.comment() == ReviewComment(
            path="src/app.py", line=12, body="[major] the lock is released before the write"
        )

    def test_unanchored_finding_has_no_inline_comment(self) -> None:
        f = finding(line=None, severity="nit", repro="")
        assert f.anchor == "src/app.py:0"
        assert not f.blocking
        assert f.render() == "- `src/app.py` [nit] the lock is released before the write"
        assert f.comment() is None

    def test_line_must_be_positive_and_severity_known(self) -> None:
        with pytest.raises(ValidationError):
            finding(line=0)
        with pytest.raises(ValidationError):
            finding(severity="catastrophic")


class TestReviewVerdict:
    def test_request_changes_needs_a_blocking_or_major_finding(self) -> None:
        with pytest.raises(ValidationError, match="minor findings and nits do not block"):
            request_changes(finding(severity="minor"), finding(severity="nit"))
        with pytest.raises(ValidationError, match="at least one finding"):
            request_changes()
        assert request_changes(finding(severity="blocking")).blocking
        assert request_changes(finding(severity="major")).event == "REQUEST_CHANGES"

    def test_approve_may_carry_findings_of_any_severity(self) -> None:
        verdict = approve(finding(severity="minor"), finding(severity="major"))
        assert verdict.event == "APPROVE"
        assert [f.severity for f in verdict.blocking] == ["major"]

    def test_empty_summary_rejected(self) -> None:
        with pytest.raises(ValidationError, match="summary"):
            approve(summary="   ")

    def test_unknown_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReviewVerdict.model_validate({"verdict": "approve", "summary": "s", "score": 1})

    def test_comments_are_anchored_only_and_capped(self) -> None:
        many = [finding(line=i + 1) for i in range(MAX_INLINE_COMMENTS + 5)]
        verdict = approve(finding(line=None), *many)
        comments = verdict.comments()
        assert len(comments) == MAX_INLINE_COMMENTS
        assert [c.line for c in comments] == list(range(1, MAX_INLINE_COMMENTS + 1))


class TestReviewBody:
    def test_summary_unanchored_findings_and_provenance(self) -> None:
        verdict = approve(finding(line=None, body="README is stale", severity="minor"))
        body = review_body(verdict, run_id="r1abc", round=2)
        assert body.startswith("looked for the four; clean")
        assert "Findings without a line anchor:\n- `src/app.py` [minor] README is stale" in body
        assert body.endswith("<sub>sbxloop review round 2 of run `r1abc`</sub>")
        assert "further inline comment" not in body

    def test_overflow_past_the_cap_is_named_not_dropped_silently(self) -> None:
        verdict = approve(*[finding(line=i + 1) for i in range(MAX_INLINE_COMMENTS + 3)])
        body = review_body(verdict, run_id="r1", round=1)
        assert f"3 further inline comment(s) were not posted (cap {MAX_INLINE_COMMENTS})" in body


class TestRefutedAnchors:
    def test_a_refuted_line_naming_the_path_marks_the_finding(self) -> None:
        verdict = request_changes(finding(), finding(path="src/db.py", line=3))
        rounds = [
            ReviewRound(
                1,
                verdict,
                "Summary of the fix.\n\n"
                "addressed: src/app.py:12 — moved the write under the lock\n"
                "refuted: src/db.py:3 — the connection is per-thread, no race",
            )
        ]
        assert refuted_anchors(rounds) == {"src/db.py:3"}

    def test_no_refutation_lines_means_nothing_refuted(self) -> None:
        rounds = [ReviewRound(1, request_changes(finding()), "addressed: src/app.py:12 — done")]
        assert refuted_anchors(rounds) == set()
        assert refuted_anchors([ReviewRound(1, request_changes(finding()), "")]) == set()

    def test_refutations_accumulate_across_rounds(self) -> None:
        rounds = [
            ReviewRound(1, request_changes(finding()), "refuted: src/app.py:12 — not a bug"),
            ReviewRound(
                2, request_changes(finding(path="b.py", line=1)), "Refuted b.py:1 as by design"
            ),
        ]
        assert refuted_anchors(rounds) == {"src/app.py:12", "b.py:1"}


class TestReconcile:
    def test_addressed_refuted_and_unanswered_with_notes(self) -> None:
        verdict = request_changes(
            finding(),
            finding(path="src/db.py", line=3),
            finding(path="src/net.py", line=9),
        )
        round = ReviewRound(
            1,
            verdict,
            "Summary of the fix.\n\n"
            "addressed: src/app.py:12 — moved the write under the lock\n"
            "refuted: src/db.py:3 — the connection is per-thread, no race",
        )
        assert reconcile(round) == {
            "src/app.py:12": ("addressed", "moved the write under the lock", ""),
            "src/db.py:3": ("refuted", "the connection is per-thread, no race", ""),
            "src/net.py:9": ("unanswered", "", ""),
        }

    def test_dash_variants_case_and_list_markers(self) -> None:
        verdict = request_changes(
            finding(),
            finding(path="src/db.py", line=3),
            finding(path="src/net.py", line=9),
        )
        round = ReviewRound(
            1,
            verdict,
            "- **Addressed**: `src/app.py:12` - swapped the order\n"
            "  * REFUTED:   src/db.py:3   \u2013 by design\n"
            "addressed: src/net.py:9: added the timeout\n",
        )
        assert reconcile(round) == {
            "src/app.py:12": ("addressed", "swapped the order", ""),
            "src/db.py:3": ("refuted", "by design", ""),
            "src/net.py:9": ("addressed", "added the timeout", ""),
        }

    def test_unknown_anchors_and_malformed_lines_are_ignored(self) -> None:
        verdict = request_changes(finding(), finding(path="src/db.py", line=3))
        round = ReviewRound(
            1,
            verdict,
            "addressed: other/file.py:99 — not a finding of this round\n"
            "refuted\n"
            "   \n"
            "some prose that names nothing\n"
            "addressed: src/app.py:12\n",
        )
        assert reconcile(round) == {
            "src/app.py:12": ("addressed", "", ""),
            "src/db.py:3": ("unanswered", "", ""),
        }

    def test_empty_response_leaves_every_finding_unanswered(self) -> None:
        assert reconcile(ReviewRound(1, request_changes(finding()), "")) == {
            "src/app.py:12": ("unanswered", "", "")
        }

    def test_refutation_wins_over_an_addressed_line_for_the_same_anchor(self) -> None:
        round = ReviewRound(
            1,
            request_changes(finding()),
            "addressed: src/app.py:12 — touched it\nrefuted: src/app.py:12 — actually fine",
        )
        assert reconcile(round)["src/app.py:12"] == ("refuted", "actually fine", "")

    def test_reconcile_rounds_merges_and_keeps_answers_over_silence(self) -> None:
        rounds = [
            ReviewRound(1, request_changes(finding()), "refuted: src/app.py:12 — not a bug"),
            ReviewRound(2, request_changes(finding()), "the fixer said nothing useful"),
        ]
        assert reconcile_rounds(rounds) == {"src/app.py:12": ("refuted", "not a bug", "")}


class TestRenderReviewHistory:
    def test_first_review_says_so(self) -> None:
        assert render_review_history([]) == "(first review of this pull request)"

    def test_rounds_carry_verdict_findings_and_response(self) -> None:
        rounds = [
            ReviewRound(1, request_changes(finding(), summary="round one"), "addressed it"),
            ReviewRound(2, approve(summary="round two"), ""),
        ]
        text = render_review_history(rounds)
        assert "### Round 1 — request_changes\n\nround one" in text
        assert "- `src/app.py:12` [major] the lock is released before the write" in text
        assert "The fixer's response:\n\naddressed it" in text
        assert "### Round 2 — approve\n\nround two\n\nFindings:\n- (no findings)" in text
        assert "(no fix round ran after this review)" in text


class TestReviewGuard:
    def test_trips_once_on_a_verdict_built_only_on_refuted_findings(self) -> None:
        guard = ReviewGuard({"src/app.py:12"})
        verdict = request_changes(finding())
        with pytest.raises(ValueError, match=r"already refuted.*src/app\.py:12"):
            guard.check(verdict)
        assert guard.tripped
        # The reviewer has been told and insists: the second one is accepted.
        guard.check(verdict)

    def test_a_new_blocking_finding_passes(self) -> None:
        guard = ReviewGuard({"src/app.py:12"})
        guard.check(request_changes(finding(), finding(path="new.py", line=1)))
        assert not guard.tripped

    def test_approvals_and_empty_refutations_never_trip(self) -> None:
        ReviewGuard({"src/app.py:12"}).check(approve(finding()))
        ReviewGuard(set()).check(request_changes(finding()))

    def test_refuted_nits_alongside_a_real_blocker_do_not_count(self) -> None:
        guard = ReviewGuard({"src/app.py:12"})
        guard.check(request_changes(finding(severity="nit"), finding(path="x.py", line=2)))
        assert not guard.tripped

    def test_a_blocking_finding_without_a_repro_is_sent_back_once(self) -> None:
        """#521: the reviewer reproduced it; the fixer needs that repro as a
        test. Sent back once with the anchors named, then accepted."""
        guard = ReviewGuard(set())
        bare = finding().model_copy(update={"repro": ""})
        with pytest.raises(ValueError, match=r"carry no `repro` \(src/app\.py:12\)"):
            guard.check(request_changes(bare))
        assert guard.tripped
        guard.check(request_changes(bare))  # told once; the second is accepted

    def test_nits_and_minors_need_no_repro(self) -> None:
        guard = ReviewGuard(set())
        guard.check(approve(finding(severity="nit").model_copy(update={"repro": ""})))
        guard.check(approve(finding(severity="minor").model_copy(update={"repro": ""})))
        assert not guard.tripped

    def test_one_trip_in_total(self) -> None:
        """The acceptance path retries exactly once: after the refuted rule
        trips, a missing repro on the retry must not fail the run."""
        guard = ReviewGuard({"src/app.py:12"})
        with pytest.raises(ValueError, match="already refuted"):
            guard.check(request_changes(finding()))
        guard.check(request_changes(finding(path="x.py", line=2).model_copy(update={"repro": ""})))


class TestFixBrief:
    def test_review_findings_section(self) -> None:
        brief = fix_brief(
            pr_number=9,
            kind="review",
            why="the review requested changes",
            round=1,
            findings=[finding()],
        )
        assert brief.startswith(
            "Pull request #9 is not yet acceptable (fix round 1, review): "
            "the review requested changes."
        )
        assert "do not start over" in brief
        assert "The review's findings:\n- `src/app.py:12` [major] the lock" in brief
        assert "may be refuted, but only with a specific reason" in brief
        assert brief.endswith(
            "a finding you refuted with a stated reason will not be raised again "
            "without a rebuttal."
        )
        assert "`addressed: <path:line> — what changed; test: <the regression test" in brief
        assert "Failing checks" not in brief
        assert "Review comments a human left" not in brief
        assert "Earlier fix rounds" not in brief

    def test_a_repro_becomes_a_required_failing_test_and_the_neighbourhood_is_asked(
        self,
    ) -> None:
        """#521: the reviewer's reproduction reaches the fixer as a test to
        write first, and the brief asks for the adjacent cases the same
        path sees — the two halves of "one adjacent case per round"."""
        brief = fix_brief(
            pr_number=9,
            kind="review",
            why="the review requested changes",
            round=2,
            findings=[
                finding(repro="a raw row with id 'gh:7', state 'running'; migrate() deletes it"),
                finding(path="src/other.py", line=3, severity="minor", repro=""),
            ],
        )
        assert (
            "- `src/app.py:12` [major] the lock is released before the write\n"
            "  Repro: a raw row with id 'gh:7', state 'running'; migrate() deletes it" in brief
        )
        assert (
            "- `src/other.py:3` [minor]" in brief
            and "src/other.py:3` [minor]\n  Repro" not in brief
        )
        assert "Reproduce it first, as a test that fails on the current tree" in brief
        assert "not through the code path under test" in brief
        assert "list the other inputs this same code path sees" in brief
        assert "row states, id forms, config shapes or error paths" in brief

    def test_without_a_repro_no_test_instruction_but_still_the_neighbourhood(self) -> None:
        brief = fix_brief(
            pr_number=9, kind="review", why="x", round=1, findings=[finding(repro="")]
        )
        assert "Reproduce it first" not in brief
        assert "list the other inputs this same code path sees" in brief

    def test_history_precedes_the_findings(self) -> None:
        brief = fix_brief(
            pr_number=9,
            kind="review",
            why="x",
            round=3,
            findings=[finding()],
            history="### Round 1 — request_changes\n\n- `a.py:1` [major] b\n  → addressed — c",
        )
        head = brief.index("Earlier fix rounds on this pull request")
        assert "build on those decisions rather than re-deriving" in brief
        assert (
            head
            < brief.index("### Round 1 — request_changes")
            < brief.index("The review's findings:")
        )

    def test_failed_checks_quote_their_log_excerpt(self) -> None:
        brief = fix_brief(
            pr_number=9,
            kind="ci",
            why="1 of 2 check(s) failed: lint",
            round=2,
            failed_checks=[
                FailedCheck("lint", "failure", "E501 line too long (src/app.py:3)", "https://x"),
                FailedCheck("test", "timed_out", "   ", "https://y"),
            ],
        )
        assert "Failing checks, with their log output:" in brief
        assert "#### `lint` (failure)\n\n```\nE501 line too long (src/app.py:3)\n```" in brief
        assert "#### `test` (timed_out)\n\n```\n(no log output was available)\n```" in brief
        assert "run the project's own gate here before you finish" in brief

    def test_human_objections_are_quoted_verbatim(self) -> None:
        brief = fix_brief(
            pr_number=9,
            kind="human",
            why="a reviewer requested changes",
            round=3,
            objections="please rename foo\n\n- `a.py:3`: and this",
        )
        assert "Review comments a human left on the PR, quoted verbatim:" in brief
        assert "please rename foo\n\n- `a.py:3`: and this" in brief
        assert "with a change, or with a reasoned explanation" in brief

    def test_before_the_first_delivery_there_is_no_pr_to_name(self) -> None:
        brief = fix_brief(pr_number=None, kind="gate", why="`make check` failed", round=1)
        assert brief.startswith("The work in this tree is not yet acceptable (fix round 1, gate)")
        assert "#None" not in brief


class TestFixTask:
    def test_ids_title_criteria_and_verify_union(self) -> None:
        spec = fix_task(
            round=2,
            pr_number=9,
            brief="fix it",
            verify_commands=["uv run pytest -q", "make check", "uv run pytest -q"],
            failed_checks=[FailedCheck("lint", "failure", "", "")],
        )
        assert spec.id == f"{FIX_TASK_PREFIX}2" == "fix-2"
        assert spec.title == FIX_TASK_TITLE
        assert spec.description == "fix it"
        assert spec.depends_on == []
        assert spec.verify_commands == ["uv run pytest -q", "make check"]
        assert spec.acceptance_criteria == [
            "PR #9's checks pass",
            "every finding is addressed or refuted",
            "the `lint` check passes",
        ]

    def test_gate_round_criteria_name_the_gate(self) -> None:
        spec = fix_task(round=1, pr_number=None, brief="b", verify_commands=["make check"])
        assert spec.acceptance_criteria[0] == "the project gate passes"

    def test_is_fix_task(self) -> None:
        assert is_fix_task("fix-1") and is_fix_task("fix-12")
        assert not is_fix_task("t1") and not is_fix_task("prefix-1")


def test_review_body_unanchored_lists_every_finding() -> None:
    from sbxloop.engine.review import ReviewFinding, ReviewVerdict, review_body

    verdict = ReviewVerdict(
        verdict="approve",
        summary="fine",
        findings=[
            ReviewFinding(path="a.py", line=3, body="anchored nit", severity="nit"),
            ReviewFinding(path="b.py", body="unanchored", severity="minor"),
        ],
    )
    body = review_body(verdict, run_id="r1", round=2, anchored=False)
    assert body.startswith("fine")
    assert "Findings:\n- `a.py:3` [nit] anchored nit\n- `b.py` [minor] unanchored" in body
    assert body.endswith("<sub>sbxloop review round 2 of run `r1`</sub>")
    # the anchored shape lists only what has no inline comment
    anchored = review_body(verdict, run_id="r1", round=2)
    assert "anchored nit" not in anchored and "unanchored" in anchored


class TestRepro:
    """#521: a finding carries the reviewer's reproduction; the fixer names
    the regression test it wrote; the next fixer sees both."""

    def test_render_and_comment_carry_the_repro(self) -> None:
        f = finding(repro="  raw row id 'gh:7'\n  state running;\n migrate() deletes it ")
        assert f.render() == (
            "- `src/app.py:12` [major] the lock is released before the write\n"
            "  Repro: raw row id 'gh:7' state running; migrate() deletes it"
        )
        assert f.render(repro=False) == (
            "- `src/app.py:12` [major] the lock is released before the write"
        )
        comment = f.comment()
        assert comment is not None
        assert comment.body == (
            "[major] the lock is released before the write\n\n"
            "**Repro:** raw row id 'gh:7' state running; migrate() deletes it"
        )
        bare = finding(repro="")
        assert "Repro" not in bare.render() and "Repro" not in bare.comment().body  # type: ignore[union-attr]
        assert bare.needs_repro and not finding(severity="nit", repro="").needs_repro

    def test_repro_is_optional_in_the_parsed_verdict(self) -> None:
        verdict = ReviewVerdict.model_validate(
            {
                "verdict": "approve",
                "summary": "fine",
                "findings": [{"path": "a.py", "line": 1, "body": "b", "severity": "nit"}],
            }
        )
        assert verdict.findings[0].repro == ""

    def test_split_test_takes_the_tail_off_the_note(self) -> None:
        assert split_test("re-keyed the row; test: tests/unit/test_store.py::test_raw_row") == (
            "re-keyed the row",
            "tests/unit/test_store.py::test_raw_row",
        )
        assert split_test("re-keyed the row (test: tests/test_x.py::test_y)") == (
            "re-keyed the row",
            "tests/test_x.py::test_y",
        )
        assert split_test("re-keyed the row, tests: `test_x.py::test_y`.") == (
            "re-keyed the row",
            "test_x.py::test_y",
        )
        assert split_test("re-keyed the row.") == ("re-keyed the row", "")
        assert split_test("") == ("", "")

    def test_reconcile_records_the_named_test(self) -> None:
        rnd = ReviewRound(
            1,
            request_changes(finding(), finding(path="src/other.py", line=3)),
            "Summary.\n\n"
            "- addressed: src/app.py:12 — take the lock first; test: tests/test_app.py::test_lock\n"
            "- refuted: src/other.py:3 — the caller holds it",
        )
        items = reconcile(rnd)
        assert items["src/app.py:12"] == (
            "addressed",
            "take the lock first",
            "tests/test_app.py::test_lock",
        )
        assert (
            items["src/app.py:12"].text
            == "take the lock first (test: `tests/test_app.py::test_lock`)"
        )
        assert items["src/other.py:3"] == ("refuted", "the caller holds it", "")
        assert items["src/other.py:3"].text == "the caller holds it"

    def test_fix_history_shows_each_findings_fate_and_test(self) -> None:
        rounds = [
            ReviewRound(
                1,
                request_changes(finding(), finding(path="src/other.py", line=3)),
                "- addressed: src/app.py:12 — lock first; test: tests/test_app.py::test_lock\n"
                "- refuted: src/other.py:3 — the caller holds it",
            ),
            ReviewRound(2, request_changes(finding(path="src/third.py", line=9)), ""),
        ]
        text = render_fix_history(rounds)
        assert text.startswith("### Round 1 — request_changes\n\n")
        assert (
            "- `src/app.py:12` [major] the lock is released before the write\n"
            "  → addressed — lock first (test: `tests/test_app.py::test_lock`)" in text
        )
        assert "- `src/other.py:3` [major]" in text and "→ refuted — the caller holds it" in text
        assert "Repro:" not in text, "the fixer gets the fate, not the old repro"
        assert "Round 2" not in text, "the open round is the brief itself"
        assert render_fix_history([]) == "" and render_fix_history(rounds[1:]) == ""

    def test_fix_history_marks_unanswered(self) -> None:
        rounds = [ReviewRound(1, request_changes(finding()), "I fixed things.")]
        assert "→ unanswered" in render_fix_history(rounds)
