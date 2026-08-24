"""Turning a review run's output into a review on the pull request.

The loop used to review its own work by filing issues — a charter issue per
delivered PR and then one backlog issue per finding:

    PR #389 → #391 (charter) → #392, #393, #394, #395, #396
    PR #375 → #378 (charter) → #379 to #383, two of them duplicates

Feedback about a diff, filed where the diff is not, with nothing to converge
on. It goes on the PR now. The parse is defensive because the JSON is
agent-authored, and its most important property is what it does *not* do:
an unusable review is never read as an approval.
"""

from __future__ import annotations

import json
from pathlib import Path

from sbxloop.daemon.review import (
    MAX_INLINE_COMMENTS,
    REVIEW_INSTRUCTIONS,
    collect_review,
    parse_review,
    review_body,
)
from sbxloop.engine.model import RunRecord
from sbxloop.gh.ops import SubmittedReview


def review_json(**over: object) -> str:
    payload: dict[str, object] = {
        "verdict": "request_changes",
        "summary": "two problems",
        "comments": [{"path": "a.py", "line": 4, "body": "off by one"}],
    }
    payload.update(over)
    return json.dumps(payload)


class TestReviewInstructions:
    """The review contract's load-bearing phrases.

    The lenses exist because the pipeline's task-completion critics were
    removed in favour of this one adversarial pass: the defect classes that
    field-verified as leaking to outside reviewers (races, failure ordering,
    unguarded parses, broken callers) must be named, not implied.
    """

    def test_the_adversarial_lenses_are_named(self) -> None:
        for marker in (
            "Concurrency and locking",
            "TOCTOU",
            "Failure ordering and partial writes",
            "Input validation at trust boundaries",
            "Cross-module interaction",
            "every caller",
        ):
            assert marker in REVIEW_INSTRUCTIONS, marker

    def test_a_green_gate_is_not_sufficient(self) -> None:
        assert "necessary, not sufficient" in REVIEW_INSTRUCTIONS

    def test_the_json_contract_is_intact(self) -> None:
        for marker in (
            "`.sbxloop/review.json`",
            '`"approve"` or `"request_changes"`',
            "{path, line, body}",
            "a clean review is a valid result",
        ):
            assert marker in REVIEW_INSTRUCTIONS, marker


class TestParseReview:
    def test_a_full_review_parses(self) -> None:
        result = parse_review(review_json())
        assert result is not None
        assert result.event == "REQUEST_CHANGES"
        assert result.summary == "two problems"
        assert [(c.path, c.line, c.body) for c in result.comments] == [("a.py", 4, "off by one")]

    def test_approve_needs_no_comments(self) -> None:
        result = parse_review(review_json(verdict="approve", summary="clean", comments=[]))
        assert result is not None and result.event == "APPROVE" and result.comments == ()

    def test_verdict_synonyms_are_accepted(self) -> None:
        """The agent writes this by hand; near-misses on the vocabulary
        should not spend a review."""
        for word in ("approve", "approved", "accept"):
            assert parse_review(review_json(verdict=word)).event == "APPROVE"  # type: ignore[union-attr]
        for word in ("request_changes", "request-changes", "reject", "revise"):
            assert parse_review(review_json(verdict=word)).event == "REQUEST_CHANGES"  # type: ignore[union-attr]

    def test_an_unusable_review_is_never_an_approval(self) -> None:
        """The property that matters most. Leaving the PR un-reviewed is
        visibly unfinished; defaulting to approve waves work through."""
        assert parse_review("not json at all") is None
        assert parse_review("[]") is None
        assert parse_review(json.dumps({"summary": "no verdict"})) is None
        assert parse_review(review_json(verdict="maybe?")) is None

    def test_a_verdict_with_nothing_behind_it_is_not_a_review(self) -> None:
        assert parse_review(json.dumps({"verdict": "approve"})) is None
        assert parse_review(review_json(summary="", comments=[])) is None

    def test_one_bad_comment_does_not_sink_the_review(self) -> None:
        result = parse_review(
            review_json(
                comments=[
                    {"path": "a.py", "body": "no line"},
                    {"line": 3, "body": "no path"},
                    {"path": "b.py", "line": 0, "body": "line 0 is not a line"},
                    "not even a dict",
                    {"path": "c.py", "line": 9, "body": "keeper"},
                ]
            )
        )
        assert result is not None
        assert [c.path for c in result.comments] == ["c.py"]

    def test_a_deleted_line_can_be_annotated_on_the_left(self) -> None:
        result = parse_review(
            review_json(comments=[{"path": "a.py", "line": 2, "body": "why", "side": "left"}])
        )
        assert result is not None and result.comments[0].side == "LEFT"

    def test_overflow_is_capped_and_counted_not_dropped_silently(self) -> None:
        many = [
            {"path": "a.py", "line": i + 1, "body": f"n{i}"} for i in range(MAX_INLINE_COMMENTS + 4)
        ]
        result = parse_review(review_json(comments=many))
        assert result is not None
        assert len(result.comments) == MAX_INLINE_COMMENTS
        assert result.dropped == 4
        assert "4 further inline comment(s)" in review_body(result, origin_run_id="r1")

    def test_the_body_carries_provenance(self) -> None:
        result = parse_review(review_json())
        assert result is not None
        assert "r1abcdefg" in review_body(result, origin_run_id="r1abcdefg")


class StubOps:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str, str, int]] = []

    def pr_review_create(
        self, repo: str, number: int, event: str, body: str, comments: object = ()
    ) -> SubmittedReview:
        self.calls.append((repo, number, event, body, len(list(comments))))  # type: ignore[arg-type]
        return SubmittedReview("https://gh/review/1", event)  # type: ignore[arg-type]


def run_record(workspace: Path | None, *, mounted: bool = True) -> RunRecord:
    return RunRecord(
        run_id="rrev00001",
        outcome="review PR #7",
        state="completed",
        created_at=0.0,
        updated_at=0.0,
        workspace=workspace,
        mounted=mounted,
    )


class TestCollectReview:
    def _workspace(self, tmp_path: Path, text: str) -> Path:
        (tmp_path / ".sbxloop").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".sbxloop" / "review.json").write_text(text)
        return tmp_path

    def test_the_review_reaches_the_pr(self, tmp_path: Path) -> None:
        ops = StubOps()
        posted = collect_review(
            run_record(self._workspace(tmp_path, review_json())),
            ops=ops,  # type: ignore[arg-type]
            repo="o/r",
            pr_number=7,
            origin_run_id="r1abcdefg",
        )
        assert posted is not None and posted.event == "REQUEST_CHANGES"
        repo, number, event, body, comments = ops.calls[0]
        assert (repo, number, event, comments) == ("o/r", 7, "REQUEST_CHANGES", 1)
        assert "two problems" in body

    def test_no_review_file_posts_nothing(self, tmp_path: Path) -> None:
        ops = StubOps()
        assert (
            collect_review(
                run_record(tmp_path),
                ops=ops,
                repo="o/r",
                pr_number=7,
                origin_run_id="r1",  # type: ignore[arg-type]
            )
            is None
        )
        assert ops.calls == []

    def test_an_unparseable_review_posts_nothing(self, tmp_path: Path) -> None:
        """Not an approval, and not a garbled review on the PR either."""
        ops = StubOps()
        assert (
            collect_review(
                run_record(self._workspace(tmp_path, "{{{")),
                ops=ops,  # type: ignore[arg-type]
                repo="o/r",
                pr_number=7,
                origin_run_id="r1",
            )
            is None
        )
        assert ops.calls == []

    def test_an_unmounted_run_is_skipped(self, tmp_path: Path) -> None:
        """`.sbxloop` never travels in the delivery, so an unmounted run has
        nowhere the file could have survived."""
        ops = StubOps()
        assert (
            collect_review(
                run_record(self._workspace(tmp_path, review_json()), mounted=False),
                ops=ops,  # type: ignore[arg-type]
                repo="o/r",
                pr_number=7,
                origin_run_id="r1",
            )
            is None
        )
        assert ops.calls == []
