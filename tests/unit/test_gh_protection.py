"""``read_base_requirements``: the base's merge requirements from classic
protection and rulesets, never raising, unknown when it cannot tell."""

from __future__ import annotations

from typing import Any

from sbxloop.errors import GithubOpsError
from sbxloop.gh.protection import BaseRequirements, read_base_requirements

UNPROTECTED = GithubOpsError("no protection", http_status=404)
FORBIDDEN = GithubOpsError("admin only", http_status=403)


class Ops:
    def __init__(self, protection: Any, rules: Any) -> None:
        self.protection, self.rules = protection, rules
        self.paths: list[str] = []

    def raw(self, method: str, path: str) -> Any:
        self.paths.append(path)
        answer = self.protection if path.endswith("/protection") else self.rules
        if isinstance(answer, Exception):
            raise answer
        return answer


def read(protection: Any, rules: Any) -> BaseRequirements:
    return read_base_requirements(Ops(protection, rules), "o/r", "main")


class TestRequiredContexts:
    def test_both_read_nothing_declared_is_an_empty_answer(self) -> None:
        assert read(UNPROTECTED, []) == BaseRequirements((), False, "none")
        assert read({}, []) == BaseRequirements((), False, "none")

    def test_classic_legacy_contexts_and_checks_forms_are_pooled(self) -> None:
        protection = {
            "required_status_checks": {
                "contexts": ["ci", "lint"],
                "checks": [{"context": "ci", "app_id": 1}, {"context": "docs"}],
            }
        }
        got = read(protection, [])
        assert got.required_contexts == ("ci", "lint", "docs")
        assert got.source == "protection"

    def test_rulesets_required_status_checks(self) -> None:
        rules = [
            {"type": "deletion"},
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": "ci"}, {"context": "coverage"}]
                },
            },
        ]
        got = read(UNPROTECTED, rules)
        assert got.required_contexts == ("ci", "coverage")
        assert got.source == "rulesets"

    def test_both_sources_are_merged_and_deduped(self) -> None:
        protection = {"required_status_checks": {"contexts": ["ci"]}}
        rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "ci"}, {"context": "lint"}]},
            }
        ]
        got = read(protection, rules)
        assert got.required_contexts == ("ci", "lint")
        assert got.source == "protection+rulesets"

    def test_an_unreadable_classic_source_is_unknown_not_empty(self) -> None:
        got = read(FORBIDDEN, [])
        assert got.required_contexts is None
        assert got.source == "unknown"

    def test_an_unreadable_rulesets_source_is_unknown(self) -> None:
        assert read({}, GithubOpsError("boom", http_status=500)).source == "unknown"
        # a non-list answer is not an answer either
        assert read({}, {"message": "moved"}).source == "unknown"

    def test_malformed_payloads_do_not_raise(self) -> None:
        got = read(
            {"required_status_checks": {"contexts": "ci", "checks": [None, {"context": ""}]}},
            [None, {"type": "required_status_checks", "parameters": None}],
        )
        assert got == BaseRequirements((), False, "none")

    def test_reads_both_endpoints_of_the_base(self) -> None:
        ops = Ops(UNPROTECTED, [])
        read_base_requirements(ops, "o/r", "release/2")
        assert ops.paths == [
            "/repos/o/r/branches/release/2/protection",
            "/repos/o/r/rules/branches/release/2",
        ]


class TestRequiresReviews:
    def test_a_positive_answer_from_either_source_is_conclusive(self) -> None:
        classic = {"required_pull_request_reviews": {"required_approving_review_count": 1}}
        assert read(classic, FORBIDDEN).requires_reviews is True
        rule = [{"type": "pull_request", "parameters": {"required_approving_review_count": 2}}]
        assert read(FORBIDDEN, rule).requires_reviews is True

    def test_false_needs_both_sources_read(self) -> None:
        assert read(UNPROTECTED, []).requires_reviews is False
        assert read(FORBIDDEN, []).requires_reviews is None
        assert read(UNPROTECTED, FORBIDDEN).requires_reviews is None

    def test_a_zero_count_does_not_require_reviews(self) -> None:
        classic = {"required_pull_request_reviews": {"required_approving_review_count": 0}}
        assert read(classic, [{"type": "pull_request", "parameters": {}}]).requires_reviews is False
