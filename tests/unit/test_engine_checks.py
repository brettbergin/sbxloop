"""``judge_checks`` (#611): whose red is it, and does it gate?"""

from __future__ import annotations

from sbxloop.config import LandingConfig
from sbxloop.engine.checks import (
    NO_POLICY,
    CheckPolicy,
    check_policy_reader,
    judge_checks,
    merged_over_comment,
    read_check_policy,
)
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import ChecksVerdict
from sbxloop.gh.protection import BaseRequirements
from tests.fakes.fake_github import GREEN, NO_CHECKS, FakeGithub


def verdict(
    *,
    failed: tuple[str, ...] = (),
    pending: tuple[str, ...] = (),
    passed: tuple[str, ...] = (),
) -> ChecksVerdict:
    state = "red" if failed else ("pending" if pending else "green")
    return ChecksVerdict(state, len(failed) + len(pending) + len(passed), pending, failed, passed)


def requires(*contexts: str, source: str = "protection") -> BaseRequirements:
    return BaseRequirements(contexts, False, source if contexts else "none")


NONE_DECLARED = requires()


class TestWithoutABaseline:
    """`NO_POLICY`: what the loop did before #611 — every check gates,
    every red is the PR's."""

    def test_green_is_green(self) -> None:
        j = judge_checks(verdict(passed=("ci",)))
        assert j.state == "green"
        assert j.source == "all"
        assert j.gating == ("ci",)
        assert not j.noteworthy

    def test_every_red_is_a_regression_to_fix(self) -> None:
        j = judge_checks(verdict(failed=("ci", "lint"), passed=("docs",)))
        assert j.state == "red"
        assert j.fix == ("ci", "lint")
        assert j.regressions == ("ci", "lint")
        assert j.preexisting == ()
        assert j.summary() == "2 of 3 check(s) failed: ci, lint"

    def test_pending_waits(self) -> None:
        j = judge_checks(verdict(pending=("ci",), passed=("lint",)))
        assert j.state == "pending"
        assert j.pending == ("ci",)
        assert j.summary() == "1 gating check(s) still to report: ci"

    def test_no_checks_at_all_is_green(self) -> None:
        assert judge_checks(NO_CHECKS).state == "green"


class TestWhoseRedIsIt:
    def test_red_on_the_baseline_too_is_preexisting_and_merged_over(self) -> None:
        policy = CheckPolicy(
            requirements=NONE_DECLARED,
            baseline_sha="base123",
            baseline=verdict(failed=("flaky",), passed=("ci",)),
        )
        j = judge_checks(verdict(failed=("flaky",), passed=("ci",)), policy)
        assert j.state == "green"
        assert j.preexisting == ("flaky",)
        assert j.fix == ()
        assert j.merged_over == ("flaky",)
        assert j.noteworthy
        assert (
            j.summary() == "nothing the pull request caused is red; merged over 1 check(s): flaky"
        )

    def test_green_on_the_baseline_is_a_regression(self) -> None:
        policy = CheckPolicy(
            requirements=NONE_DECLARED, baseline_sha="base123", baseline=verdict(passed=("ci",))
        )
        j = judge_checks(verdict(failed=("ci",)), policy)
        assert j.state == "red"
        assert j.regressions == ("ci",)
        assert j.fix == ("ci",)

    def test_absent_from_the_baseline_is_ours_fail_closed(self) -> None:
        policy = CheckPolicy(requirements=NONE_DECLARED, baseline_sha="base123", baseline=NO_CHECKS)
        j = judge_checks(verdict(failed=("pr-only",)), policy)
        assert j.fix == ("pr-only",)

    def test_an_unreadable_baseline_makes_every_red_ours(self) -> None:
        policy = CheckPolicy(requirements=NONE_DECLARED, baseline_sha=None, baseline=None)
        j = judge_checks(verdict(failed=("flaky",)), policy)
        assert j.fix == ("flaky",)
        assert j.preexisting == ()

    def test_mixed_reds_summarise_each_kind(self) -> None:
        policy = CheckPolicy(
            requirements=NONE_DECLARED,
            baseline_sha="base123",
            baseline=verdict(failed=("flaky",)),
        )
        j = judge_checks(verdict(failed=("ci", "flaky")), policy)
        assert j.state == "red"
        assert j.summary() == "1 check(s) failed: ci; already red on the base: flaky"


class TestDoesItGate:
    def test_declared_contexts_gate_and_the_rest_are_advisory(self) -> None:
        policy = CheckPolicy(requirements=requires("ci"), baseline_sha="b", baseline=NO_CHECKS)
        j = judge_checks(verdict(failed=("lint",), passed=("ci",)), policy)
        assert j.gating == ("ci",)
        assert j.source == "protection"
        # an advisory regression still gets its one round...
        assert j.state == "red"
        assert j.fix == ("lint",)
        assert j.advisory_only

    def test_an_advisory_regression_past_its_round_is_merged_over(self) -> None:
        policy = CheckPolicy(
            requirements=requires("ci"),
            baseline_sha="b",
            baseline=NO_CHECKS,
            advisory_spent=frozenset({"lint"}),
        )
        j = judge_checks(verdict(failed=("lint",), passed=("ci",)), policy)
        assert j.state == "green"
        assert j.advisory == ("lint",)
        assert j.merged_over == ("lint",)
        assert not j.advisory_only

    def test_a_declared_requirement_red_on_the_base_is_still_fix_scope(self) -> None:
        # GitHub will refuse the merge whatever the base looks like.
        policy = CheckPolicy(
            requirements=requires("ci"), baseline_sha="b", baseline=verdict(failed=("ci",))
        )
        j = judge_checks(verdict(failed=("ci",)), policy)
        assert j.state == "red"
        assert j.preexisting == ("ci",)
        assert j.fix == ("ci",)
        assert j.merged_over == ()
        assert not j.advisory_only

    def test_gating_reds_get_the_full_rounds_even_when_advisory_is_spent(self) -> None:
        policy = CheckPolicy(
            requirements=requires("ci"),
            baseline_sha="b",
            baseline=NO_CHECKS,
            advisory_spent=frozenset({"lint", "ci"}),
        )
        j = judge_checks(verdict(failed=("ci", "lint")), policy)
        assert j.fix == ("ci",)
        assert j.advisory == ("lint",)
        assert j.summary() == "1 check(s) failed: ci; advisory, past their round: lint"

    def test_only_gating_checks_are_waited_on(self) -> None:
        policy = CheckPolicy(requirements=requires("ci"), baseline_sha="b", baseline=NO_CHECKS)
        j = judge_checks(verdict(pending=("slow-e2e",), passed=("ci",)), policy)
        assert j.state == "green"
        assert j.pending == ()

    def test_a_declared_context_absent_from_the_head_is_pending(self) -> None:
        policy = CheckPolicy(
            requirements=requires("ci", "docs"), baseline_sha="b", baseline=NO_CHECKS
        )
        j = judge_checks(verdict(passed=("ci",)), policy)
        assert j.state == "pending"
        assert j.pending == ("docs",)

    def test_required_checks_config_overrides_what_the_base_declares(self) -> None:
        policy = CheckPolicy(
            requirements=requires("ci"), baseline_sha="b", baseline=NO_CHECKS, required=("lint",)
        )
        j = judge_checks(verdict(failed=("ci",), passed=("lint",)), policy)
        assert j.gating == ("lint",)
        assert j.source == "config"
        assert j.fix == ("ci",)  # advisory, first round
        assert j.advisory_only

    def test_unknown_requirements_gate_on_everything(self) -> None:
        policy = CheckPolicy(baseline_sha="b", baseline=NO_CHECKS)
        j = judge_checks(verdict(pending=("anything",), passed=("ci",)), policy)
        assert j.source == "all"
        assert j.state == "pending"

    def test_ignore_checks_removes_a_name_everywhere(self) -> None:
        policy = CheckPolicy(
            requirements=requires("ci", "codecov/patch"),
            baseline_sha="b",
            baseline=NO_CHECKS,
            ignore=("codecov/*",),
        )
        j = judge_checks(
            verdict(failed=("codecov/patch",), pending=("codecov/project",), passed=("ci",)), policy
        )
        assert j.state == "green"
        assert j.ignored == ("codecov/patch", "codecov/project")
        assert j.gating == ("ci",)
        assert j.noteworthy

    def test_event_payload(self) -> None:
        policy = CheckPolicy(
            requirements=requires("ci"), baseline_sha="base123", baseline=verdict(failed=("old",))
        )
        j = judge_checks(verdict(failed=("old",), passed=("ci",)), policy)
        assert j.event() == {
            "state": "green",
            "required": ["ci"],
            "source": "protection",
            "pending": [],
            "needs_approval": [],
            "fix": [],
            "regressions": [],
            "preexisting": ["old"],
            "advisory": [],
            "ignored": [],
            "baseline_sha": "base123",
        }


class TestMergedOverComment:
    def test_nothing_merged_over_means_no_comment(self) -> None:
        assert merged_over_comment(judge_checks(GREEN)) is None

    def test_names_each_red_and_why_it_was_merged_over(self) -> None:
        policy = CheckPolicy(
            requirements=requires("ci"),
            baseline_sha="base123abcdef0123",
            baseline=verdict(failed=("flaky",)),
            advisory_spent=frozenset({"lint"}),
        )
        j = judge_checks(verdict(failed=("flaky", "lint"), passed=("ci",)), policy)
        assert merged_over_comment(j) == (
            "Merged with checks still red that this pull request did not cause:\n"
            "\n"
            "- `flaky` — already red on base123abcde, the commit this PR is built on\n"
            "- `lint` — went red on this PR but is not required by the base branch; "
            "one fix round did not clear it"
        )


class TestReadCheckPolicy:
    def test_reads_requirements_merge_base_and_baseline(self) -> None:
        fake = FakeGithub()
        fake.protection = {"required_status_checks": {"contexts": ["ci"]}}
        fake.checks_by_sha["base123"] = verdict(failed=("flaky",))
        cfg = LandingConfig(ignore_checks=["codecov/*"])
        policy = read_check_policy(fake, "o/r", "main", "head1", cfg=cfg, advisory_spent={"x"})
        assert policy.requirements.required_contexts == ("ci",)
        assert policy.baseline_sha == "base123"
        assert policy.baseline == verdict(failed=("flaky",))
        assert policy.ignore == ("codecov/*",)
        assert policy.advisory_spent == {"x"}
        assert fake.checks_calls == ["base123"]

    def test_unrelated_histories_leave_no_baseline(self) -> None:
        fake = FakeGithub()
        fake.unrelated_branches.add("orphan")
        policy = read_check_policy(fake, "o/r", "main", "orphan", cfg=LandingConfig())
        assert policy.baseline_sha is None
        assert policy.baseline is None
        assert fake.checks_calls == []

    def test_a_github_error_on_the_baseline_is_swallowed(self) -> None:
        fake = FakeGithub()
        fake.fail_once["pr_checks"] = GithubOpsError("down", http_status=502)
        policy = read_check_policy(fake, "o/r", "main", "head1", cfg=LandingConfig())
        assert policy.baseline_sha == "base123"
        assert policy.baseline is None

    def test_the_reader_caches_requirements_once_and_the_baseline_per_head(self) -> None:
        fake = FakeGithub()
        policy_for = check_policy_reader(fake, "o/r", "main", cfg=LandingConfig())
        first = policy_for("head1")
        assert policy_for("head1") is first
        policy_for("head2")
        protection_reads = [p for _, p, _ in fake.raw_calls if p.endswith("/protection")]
        assert len(protection_reads) == 1
        assert fake.checks_calls == ["base123", "base123"]

    def test_no_policy_is_the_pre_611_judgment(self) -> None:
        assert NO_POLICY.baseline is None
        assert NO_POLICY.requirements.source == "unknown"
