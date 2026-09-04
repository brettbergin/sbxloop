"""``read_base_requirements``: the base's merge requirements from classic
protection and rulesets, never raising, unknown when it cannot tell."""

from __future__ import annotations

from typing import Any

import pytest

from sbxloop.errors import GithubOpsError
from sbxloop.gh.protection import BaseRequirements, read_base_requirements, with_pr_rollup

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
        assert read(UNPROTECTED, []) == BaseRequirements((), 0, "none")
        assert read({}, []) == BaseRequirements((), 0, "none")

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
        assert got.unread == ("protection",)
        assert read(FORBIDDEN, FORBIDDEN).unread == ("protection", "rulesets")
        assert read({}, FORBIDDEN).unread == ("rulesets",)
        assert read(UNPROTECTED, []).unread == ()

    def test_an_unreadable_rulesets_source_is_unknown(self) -> None:
        assert read({}, GithubOpsError("boom", http_status=500)).source == "unknown"
        # a non-list answer is not an answer either
        assert read({}, {"message": "moved"}).source == "unknown"

    def test_malformed_payloads_do_not_raise(self) -> None:
        got = read(
            {"required_status_checks": {"contexts": "ci", "checks": [None, {"context": ""}]}},
            [None, {"type": "required_status_checks", "parameters": None}],
        )
        assert got == BaseRequirements((), 0, "none")

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
        assert read(classic, FORBIDDEN).approvals_required == 1
        rule = [{"type": "pull_request", "parameters": {"required_approving_review_count": 2}}]
        assert read(FORBIDDEN, rule).requires_reviews is True
        assert read(FORBIDDEN, rule).approvals_required == 2

    def test_the_count_is_the_larger_of_the_two_sources(self) -> None:
        classic = {"required_pull_request_reviews": {"required_approving_review_count": 1}}
        rule = [{"type": "pull_request", "parameters": {"required_approving_review_count": 3}}]
        assert read(classic, rule).approvals_required == 3

    def test_false_needs_both_sources_read(self) -> None:
        assert read(UNPROTECTED, []).requires_reviews is False
        assert read(FORBIDDEN, []).requires_reviews is None
        assert read(UNPROTECTED, FORBIDDEN).requires_reviews is None

    def test_a_zero_count_does_not_require_reviews(self) -> None:
        classic = {"required_pull_request_reviews": {"required_approving_review_count": 0}}
        assert read(classic, [{"type": "pull_request", "parameters": {}}]).requires_reviews is False


# One fixture per rule type, in both of GitHub's dialects (#673): the
# field it must set, the classic-protection payload and the ruleset rule.
RULE_TYPES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    (
        "code_owner_review",
        {"required_pull_request_reviews": {"require_code_owner_reviews": True}},
        {"type": "pull_request", "parameters": {"require_code_owner_review": True}},
    ),
    (
        "last_push_approval",
        {"required_pull_request_reviews": {"require_last_push_approval": True}},
        {"type": "pull_request", "parameters": {"require_last_push_approval": True}},
    ),
    (
        "dismiss_stale_reviews",
        {"required_pull_request_reviews": {"dismiss_stale_reviews": True}},
        {"type": "pull_request", "parameters": {"dismiss_stale_reviews_on_push": True}},
    ),
    (
        "conversation_resolution",
        {"required_conversation_resolution": {"enabled": True}},
        {"type": "pull_request", "parameters": {"required_review_thread_resolution": True}},
    ),
    (
        "linear_history",
        {"required_linear_history": {"enabled": True}},
        {"type": "required_linear_history"},
    ),
    (
        "signed_commits",
        {"required_signatures": {"enabled": True}},
        {"type": "required_signatures"},
    ),
    ("merge_queue", {}, {"type": "merge_queue", "parameters": {"merge_method": "SQUASH"}}),
    (
        "required_deployments",
        {},
        {
            "type": "required_deployments",
            "parameters": {"required_deployment_environments": ["staging", "prod"]},
        },
    ),
]

FLAGS = [name for name, _, _ in RULE_TYPES if name != "required_deployments"]


def expected(field: str) -> Any:
    return ("staging", "prod") if field == "required_deployments" else True


class TestEveryRuleType:
    @pytest.mark.parametrize(
        ("field", "classic", "rule"), RULE_TYPES, ids=[r[0] for r in RULE_TYPES]
    )
    def test_a_ruleset_rule_sets_its_field(
        self, field: str, classic: dict[str, Any], rule: dict[str, Any]
    ) -> None:
        got = read(UNPROTECTED, [rule])
        assert getattr(got, field) == expected(field)
        # Only that field: every other flag stays off.
        for other in FLAGS:
            if other != field:
                assert getattr(got, other) is False, other
        assert got.required_deployments == (
            () if field != "required_deployments" else expected(field)
        )
        assert got.approvals_required == 0 and got.required_contexts == ()

    @pytest.mark.parametrize(
        ("field", "classic", "rule"),
        [r for r in RULE_TYPES if r[1]],
        ids=[r[0] for r in RULE_TYPES if r[1]],
    )
    def test_classic_protection_sets_the_same_field(
        self, field: str, classic: dict[str, Any], rule: dict[str, Any]
    ) -> None:
        got = read(classic, [])
        assert getattr(got, field) is True
        for other in FLAGS:
            if other != field:
                assert getattr(got, other) is False, other

    def test_a_flag_from_either_source_survives_an_unreadable_other(self) -> None:
        """A rule read is conclusive on its own; the requirements are still
        `unknown` as a whole because the other half may hide more."""
        got = read(FORBIDDEN, [{"type": "required_signatures"}])
        assert got.signed_commits is True
        assert got.source == "unknown" and got.required_contexts is None

    def test_flags_are_pooled_across_both_sources(self) -> None:
        got = read(
            {"required_linear_history": {"enabled": True}},
            [
                {"type": "merge_queue"},
                {
                    "type": "required_deployments",
                    "parameters": {"required_deployment_environments": ["prod", "prod"]},
                },
            ],
        )
        assert got.linear_history and got.merge_queue
        assert got.required_deployments == ("prod",)
        assert got.source == "none"  # no *contexts* were declared by either

    def test_a_disabled_classic_block_is_not_a_requirement(self) -> None:
        got = read({"required_signatures": {"enabled": False}, "required_linear_history": {}}, [])
        assert got.signed_commits is False and got.linear_history is False


class TestBlockers:
    def test_nothing_required_nothing_blocks(self) -> None:
        assert BaseRequirements((), 0, "none").blockers() == []
        assert BaseRequirements(None, None, "unknown").blockers() == []

    def test_last_push_approval_is_named_and_fatal(self) -> None:
        rules = BaseRequirements((), 1, "rulesets", last_push_approval=True)
        (fatal, _review) = rules.blockers()
        assert "require_last_push_approval" in fatal
        assert "always the last pusher" in fatal
        # Even with an approver on hand the last-push rule stands.
        assert rules.blockers(can_approve=True) == [fatal]

    def test_reviews_and_code_owners(self) -> None:
        assert BaseRequirements((), 1, "rulesets").blockers() == [
            "the base requires an approving review, which the loop cannot give its own pull request"
        ]
        assert (
            BaseRequirements((), 2, "rulesets")
            .blockers()[0]
            .startswith("the base requires 2 approving reviews")
        )
        assert BaseRequirements((), 1, "rulesets").blockers(can_approve=True) == []
        (owner,) = BaseRequirements((), 0, "rulesets", code_owner_review=True).blockers()
        assert "CODEOWNERS" in owner

    def test_signing_is_the_credentials_to_satisfy(self) -> None:
        rules = BaseRequirements((), 0, "rulesets", signed_commits=True)
        (why,) = rules.blockers(can_sign=False)
        assert "GitHub App" in why
        assert rules.blockers(can_sign=True) == []

    def test_linear_history_blocks_only_a_merge_commit(self) -> None:
        rules = BaseRequirements((), 0, "rulesets", linear_history=True)
        assert rules.blockers(merge_method="squash") == []
        assert rules.blockers(merge_method="rebase") == []
        assert rules.blockers() == []
        (why,) = rules.blockers(merge_method="merge")
        assert "linear history" in why and "squash or rebase" in why

    def test_deployments_block_and_a_merge_queue_does_not(self) -> None:
        """#676: the loop enqueues on a merge-queue base, so the queue is
        no longer a rule it cannot satisfy."""
        rules = BaseRequirements(
            (), 0, "rulesets", merge_queue=True, required_deployments=("staging", "prod")
        )
        (deploy,) = rules.blockers()
        assert "deployment to staging, prod" in deploy

    def test_rules_the_loop_satisfies_itself_are_not_blockers(self) -> None:
        rules = BaseRequirements(
            (), 0, "rulesets", conversation_resolution=True, dismiss_stale_reviews=True
        )
        assert rules.blockers() == []


class TestWithPrRollup:
    """#674: a base whose rules this token cannot read learns its required
    checks from the pull request's own rollup, which GitHub evaluates
    against the same rules and serves with pull access."""

    class Rollup:
        def __init__(self, required: tuple[str, ...] | Exception) -> None:
            self.required, self.calls = required, 0

        def pr_required_checks(self, repo: str, number: int) -> tuple[str, ...]:
            self.calls += 1
            if isinstance(self.required, Exception):
                raise self.required
            return self.required

    def test_fills_the_required_set_when_a_source_was_unread(self) -> None:
        unknown = read(FORBIDDEN, [])
        got = with_pr_rollup(self.Rollup(("ci",)), "o/r", 7, unknown)
        assert got.required_contexts == ("ci",)
        assert got.source == "pr-rollup"
        assert got.unread == ("protection",), "what could not be read is still on record"

    def test_the_readable_sources_rules_are_kept(self) -> None:
        rules = [{"type": "pull_request", "parameters": {"required_approving_review_count": 2}}]
        got = with_pr_rollup(self.Rollup(("ci",)), "o/r", 7, read(FORBIDDEN, rules))
        assert got.approvals_required == 2 and got.required_contexts == ("ci",)

    def test_a_readable_base_is_not_asked(self) -> None:
        rollup = self.Rollup(("ci",))
        known = read({}, [])
        assert with_pr_rollup(rollup, "o/r", 7, known) is known
        assert rollup.calls == 0

    def test_an_unreadable_rollup_leaves_the_base_unknown(self) -> None:
        unknown = read(FORBIDDEN, [])
        got = with_pr_rollup(self.Rollup(GithubOpsError("nope")), "o/r", 7, unknown)
        assert got is unknown
        assert with_pr_rollup(self.Rollup(RuntimeError("boom")), "o/r", 7, unknown) is unknown

    def test_an_empty_rollup_is_an_empty_set_the_gate_reads_as_everything(self) -> None:
        got = with_pr_rollup(self.Rollup(()), "o/r", 7, read(FORBIDDEN, []))
        assert got.required_contexts == () and got.source == "pr-rollup"
