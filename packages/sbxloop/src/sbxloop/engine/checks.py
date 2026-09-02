"""Judging a pull request's checks against its base (#611).

A red check on the PR head answers "is this build broken?", not "did this
pull request break it?". The second question is the one a fix round and a
merge decision need, and it has two halves:

* **Whose red is it?** The same checks are folded on the commit the PR is
  built on — its merge base with the base branch, never the base's *current*
  head, whose red would be someone else's. Red there too is **preexisting**:
  merged over and named, not fixed. Green or absent there is a
  **regression**: the PR's own, fixed. Absent is treated as ours on purpose
  — a check that only runs on pull requests has no baseline, and "could not
  tell" must not read as "not our fault".

* **Does it gate?** The base's protection and rulesets say which contexts
  must be green (:mod:`sbxloop.gh.protection`); the rest are advisory.
  A base that declares none — or whose rules cannot be read — gates on
  everything, which is what the loop always did. Gating reds get the full
  ``max_ci_rounds``; an advisory regression gets one round and is then
  merged over and named, so a non-human signal never blocks a landing.
  Only gating checks are waited on.

Everything here is pure: :func:`judge_checks` takes a folded verdict and a
:class:`CheckPolicy` and answers; :func:`read_check_policy` is the one
function that talks to GitHub, and the engine calls it once per stage.
"""

from __future__ import annotations

from collections.abc import Callable, Container
from fnmatch import fnmatchcase
from typing import Any, NamedTuple

from sbxloop.config import LandingConfig
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import CheckState, ChecksVerdict, GithubOps, approval_summary
from sbxloop.gh.protection import BaseRequirements, read_base_requirements
from sbxloop.log import get_logger

log = get_logger(__name__)

UNKNOWN_REQUIREMENTS = BaseRequirements(None, None, "unknown")


class CheckPolicy(NamedTuple):
    """Everything :func:`judge_checks` needs besides the head's verdict."""

    requirements: BaseRequirements = UNKNOWN_REQUIREMENTS
    # The commit the PR is built on and the checks folded there; None
    # baseline = it could not be read, and every red is then the PR's.
    baseline_sha: str | None = None
    baseline: ChecksVerdict | None = None
    # `[landing] required_checks` / `ignore_checks`.
    required: tuple[str, ...] = ()
    ignore: tuple[str, ...] = ()
    # Advisory regressions that already had their one fix round.
    advisory_spent: Container[str] = frozenset()


# What the loop did before #611: no baseline, no protection reading, every
# check gates. Also what a caller with no repository context judges by.
NO_POLICY = CheckPolicy()


class CheckJudgment(NamedTuple):
    """One verdict, judged: what to wait on, fix, and merge over."""

    # What the loop acts on: red = a fix round is due, pending = a gating
    # check has not reported, green = nothing stands in the way.
    state: CheckState
    verdict: ChecksVerdict
    # The names that gate the merge and where that set came from —
    # `config`, the requirements' source, or `all`.
    gating: tuple[str, ...]
    source: str
    # Gating checks still to report (a declared context absent from the
    # head counts: GitHub waits for it too).
    pending: tuple[str, ...]
    # Gating checks whose workflow a maintainer has not approved (#612):
    # `pending` in state, but nothing the loop can wait out or fix — the
    # pollers return on it at once and the landing hands over, named.
    needs_approval: tuple[str, ...]
    # Reds a fix round targets: gating reds plus advisory regressions on
    # their first round.
    fix: tuple[str, ...]
    # Every red not red on the baseline, gating or not.
    regressions: tuple[str, ...]
    # Reds already red on the baseline.
    preexisting: tuple[str, ...]
    # Advisory regressions past their one round: merged over, named.
    advisory: tuple[str, ...]
    ignored: tuple[str, ...]
    baseline_sha: str | None

    @property
    def merged_over(self) -> tuple[str, ...]:
        """The reds a merge would leave standing: what the PR comment names."""
        return (*(n for n in self.preexisting if n not in self.fix), *self.advisory)

    @property
    def noteworthy(self) -> bool:
        """Whether the judgment says anything beyond "every check gates and
        every one is green" — what the `landing.checks` event is for."""
        return bool(
            self.state != "green" or self.merged_over or self.ignored or self.source != "all"
        )

    @property
    def advisory_only(self) -> bool:
        """The fix scope is nothing but advisory regressions on their first
        round — a round worth spending, never worth failing a run over."""
        return bool(self.fix) and not any(n in self.gating for n in self.fix)

    def summary(self) -> str:
        if self.state == "green":
            if self.merged_over:
                return (
                    f"nothing the pull request caused is red; merged over "
                    f"{len(self.merged_over)} check(s): {', '.join(self.merged_over)}"
                )
            return self.verdict.summary()
        if self.state == "pending":
            if self.needs_approval:
                return approval_summary(self.needs_approval)
            return f"{len(self.pending)} gating check(s) still to report: {', '.join(self.pending)}"
        if self.fix == self.verdict.failed:
            return self.verdict.summary()
        parts = [f"{len(self.fix)} check(s) failed: {', '.join(self.fix)}"]
        preexisting = [n for n in self.preexisting if n not in self.fix]
        if preexisting:
            parts.append(f"already red on the base: {', '.join(preexisting)}")
        if self.advisory:
            parts.append(f"advisory, past their round: {', '.join(self.advisory)}")
        return "; ".join(parts)

    def event(self) -> dict[str, Any]:
        """The `landing.checks` payload."""
        return {
            "state": self.state,
            "required": list(self.gating),
            "source": self.source,
            "pending": list(self.pending),
            "needs_approval": list(self.needs_approval),
            "fix": list(self.fix),
            "regressions": list(self.regressions),
            "preexisting": list(self.preexisting),
            "advisory": list(self.advisory),
            "ignored": list(self.ignored),
            "baseline_sha": self.baseline_sha,
        }


def judge_checks(verdict: ChecksVerdict, policy: CheckPolicy = NO_POLICY) -> CheckJudgment:
    """Judge the head's folded checks under ``policy`` (module notes)."""
    ignored = tuple(
        dict.fromkeys(n for n in verdict.names if any(fnmatchcase(n, pat) for pat in policy.ignore))
    )
    names = [n for n in verdict.names if n not in ignored]
    failed = [n for n in verdict.failed if n not in ignored]
    pending = [n for n in verdict.pending if n not in ignored]

    declared: tuple[str, ...] | None
    if policy.required:
        declared, source = tuple(policy.required), "config"
    elif policy.requirements.required_contexts:
        declared, source = policy.requirements.required_contexts, policy.requirements.source
    else:
        declared, source = None, "all"
    gating = tuple(n for n in declared if n not in ignored) if declared else tuple(names)

    baseline_failed = set(policy.baseline.failed) if policy.baseline is not None else set()
    regressions = [n for n in failed if n not in baseline_failed]
    preexisting = [n for n in failed if n in baseline_failed]
    # A declared requirement must pass whatever the base looks like — GitHub
    # will refuse the merge otherwise. Under the "all" fallback nothing is
    # declared, so a red the base already had is the base's to fix.
    gating_red = [n for n in failed if n in gating and (declared is not None or n in regressions)]
    fresh = [n for n in regressions if n not in gating and n not in policy.advisory_spent]
    advisory = [n for n in regressions if n not in gating and n in policy.advisory_spent]
    fix = tuple(dict.fromkeys([*gating_red, *fresh]))
    # An unapproved workflow gates like a pending one — GitHub waits for
    # it too — but is reported apart, so nobody waits on it (#612). A real
    # red comes first: that round is worth spending whatever else waits.
    approval = tuple(n for n in verdict.needs_approval if n in gating and n not in ignored)
    still_pending = tuple(n for n in gating if n in pending or n in approval or n not in names)

    state: CheckState = "red" if fix else ("pending" if still_pending else "green")
    return CheckJudgment(
        state=state,
        verdict=verdict,
        gating=gating,
        source=source,
        pending=still_pending,
        needs_approval=approval,
        fix=fix,
        regressions=tuple(regressions),
        preexisting=tuple(preexisting),
        advisory=tuple(advisory),
        ignored=ignored,
        baseline_sha=policy.baseline_sha,
    )


PolicyFor = Callable[[str], CheckPolicy]


def no_policy(head: str) -> CheckPolicy:
    return NO_POLICY


def read_check_policy(
    ops: GithubOps,
    repo: str,
    base: str,
    head: str,
    *,
    cfg: LandingConfig,
    advisory_spent: Container[str] = frozenset(),
    requirements: BaseRequirements | None = None,
) -> CheckPolicy:
    """The policy for judging ``head``: the base's requirements (read here
    unless the caller hands them in) and the checks on ``head``'s merge base.

    Reads never raise. A merge base GitHub cannot name, or a baseline it
    cannot fold, leaves ``baseline`` None — and every red the PR's.
    """
    if requirements is None:
        requirements = read_base_requirements(ops, repo, base)
    baseline_sha: str | None = None
    baseline: ChecksVerdict | None = None
    try:
        baseline_sha = ops.merge_base(repo, base, head)
        if baseline_sha:
            baseline = ops.pr_checks(repo, baseline_sha)
    except GithubOpsError as exc:
        log.warning(
            "checks.baseline_unread",
            repo=repo,
            base=base,
            head=head,
            error=str(exc),
            hint="every red check on the head will be treated as this PR's own",
        )
        baseline = None
    return CheckPolicy(
        requirements=requirements,
        baseline_sha=baseline_sha,
        baseline=baseline,
        required=tuple(cfg.required_checks),
        ignore=tuple(cfg.ignore_checks),
        advisory_spent=advisory_spent,
    )


def check_policy_reader(
    ops: GithubOps,
    repo: str,
    base: str,
    *,
    cfg: LandingConfig,
    advisory_spent: Container[str] = frozenset(),
) -> PolicyFor:
    """A :data:`PolicyFor` that reads the base's requirements once and the
    baseline once per head it is asked about — ``land()`` asks per poll,
    and the head only moves on a re-delivery or an update-branch."""
    requirements: BaseRequirements | None = None
    policies: dict[str, CheckPolicy] = {}

    def policy_for(head: str) -> CheckPolicy:
        nonlocal requirements
        if head not in policies:
            if requirements is None:
                requirements = read_base_requirements(ops, repo, base)
            policies[head] = read_check_policy(
                ops,
                repo,
                base,
                head,
                cfg=cfg,
                advisory_spent=advisory_spent,
                requirements=requirements,
            )
        return policies[head]

    return policy_for


def merged_over_comment(judgment: CheckJudgment) -> str | None:
    """The PR comment naming what the merge leaves red, or None when the
    merge leaves nothing — so a human reading the PR later sees the loop
    knew, and why it went ahead."""
    if not judgment.merged_over:
        return None
    sha = judgment.baseline_sha[:12] if judgment.baseline_sha else "the base"
    lines = ["Merged with checks still red that this pull request did not cause:", ""]
    for name in judgment.preexisting:
        if name not in judgment.fix:
            lines.append(f"- `{name}` — already red on {sha}, the commit this PR is built on")
    for name in judgment.advisory:
        lines.append(
            f"- `{name}` — went red on this PR but is not required by the base branch; "
            "one fix round did not clear it"
        )
    return "\n".join(lines)
