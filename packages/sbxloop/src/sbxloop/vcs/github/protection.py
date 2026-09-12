"""What the delivery base requires of a pull request before it may merge.

GitHub keeps the answer in two places that do not know about each other:
classic branch protection (``GET /repos/{repo}/branches/{base}/protection``,
admin-only to read) and rulesets (``GET /repos/{repo}/rules/branches/{base}``,
readable by anyone who can read the repository). A repository may use
either, both, or neither, so both are read and their answers pooled.

The reading is advisory and never raises: an unreadable source is reported
as *unknown*, not as "nothing required" — the landing gate falls back to
treating every check as gating when it cannot tell (#611), and doctor says
"unverifiable" rather than "fine".

Every rule type a landing has to satisfy is read (#673), not only the
required checks and the review count: a base that wants a code-owner
review, approval of the last push, signed commits, linear history, a
merge queue or a deployment refuses the loop's merge with a bare 405, and
:meth:`BaseRequirements.blockers` is how the loop and doctor say *which*.

Classic protection being admin-only to read is the common case for an
organization bot — write, not admin — and "unknown" there used to gate on
every check, undoing the baseline comparison on exactly the repositories
it was built for (#674). GitHub itself says which of a pull request's
checks it holds the merge for, readable with pull access:
:func:`with_pr_rollup` fills the required set from the PR's own rollup
when a source could not be read.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from sbxloop.errors import GithubOpsError
from sbxloop.log import get_logger
from sbxloop.vcs.model import BaseRequirements as BaseRequirements

log = get_logger(__name__)


UNKNOWN = BaseRequirements(None, None, "unknown")


class _Reading(NamedTuple):
    """One source's answer: whether it was read, and what it declared."""

    known: bool
    contexts: list[str]
    requirements: BaseRequirements


def read_base_requirements(ops: Any, repo: str, base: str) -> BaseRequirements:
    """Read ``base``'s requirements from classic protection and rulesets.

    ``ops`` needs only ``raw(method, path)``. A classic 404 is "explicitly
    unprotected" — an answer; any other failure on either source leaves
    that source unknown. Nothing here raises: the callers are a merge gate
    that must decide something and a doctor that must not crash.
    """
    classic = _read_classic(ops, repo, base)
    rules = _read_rulesets(ops, repo, base)
    both = classic.known and rules.known

    counts = [
        r.requirements.approvals_required
        for r in (classic, rules)
        if r.requirements.approvals_required
    ]
    approvals: int | None = max(counts) if counts else (0 if both else None)
    flags = _pool(classic.requirements, rules.requirements)
    if not both:
        unread = tuple(
            name for name, r in (("protection", classic), ("rulesets", rules)) if not r.known
        )
        return BaseRequirements(None, approvals, "unknown", *flags)._replace(unread=unread)
    contexts = tuple(dict.fromkeys([*classic.contexts, *rules.contexts]))
    sources = [
        name
        for name, found in (("protection", classic.contexts), ("rulesets", rules.contexts))
        if found
    ]
    return BaseRequirements(contexts, approvals, "+".join(sources) or "none", *flags)


def with_pr_rollup(
    ops: Any, repo: str, number: int, requirements: BaseRequirements
) -> BaseRequirements:
    """``requirements`` with the required checks GitHub reports on the pull
    request itself when a source could not be read (#674).

    ``ops`` needs ``pr_required_checks(repo, number)``. The rollup lists
    only what has reported on the head, so this is asked again as checks
    arrive, and an empty answer gates on everything as before — the
    fallback in the safe direction. Rules other than checks stay as the
    readable source declared them; ``source`` becomes ``pr-rollup``.
    Nothing here raises: an unreadable rollup leaves the requirements
    unknown, as they were.
    """
    if requirements.required_contexts is not None:
        return requirements
    try:
        contexts = ops.pr_required_checks(repo, number)
    except Exception as exc:  # nosec B110 - advisory probe; the fallback is "gate on all"
        log.info("protection.rollup_unreadable", repo=repo, pr=number, error=str(exc))
        return requirements
    log.info("protection.required_from_pr", repo=repo, pr=number, required=list(contexts))
    return requirements._replace(required_contexts=tuple(contexts), source="pr-rollup")


def _pool(a: BaseRequirements, b: BaseRequirements) -> tuple[Any, ...]:
    """The flags either source declared, in field order after ``source``."""
    return (
        a.code_owner_review or b.code_owner_review,
        a.last_push_approval or b.last_push_approval,
        a.dismiss_stale_reviews or b.dismiss_stale_reviews,
        a.conversation_resolution or b.conversation_resolution,
        a.linear_history or b.linear_history,
        a.signed_commits or b.signed_commits,
        a.merge_queue or b.merge_queue,
        tuple(dict.fromkeys([*a.required_deployments, *b.required_deployments])),
    )


def _read_classic(ops: Any, repo: str, base: str) -> _Reading:
    from sbxloop.vcs.github.ops import raw_lookup

    try:
        protection = raw_lookup(ops, "GET", f"/repos/{repo}/branches/{base}/protection")
    except GithubOpsError as exc:
        log.info("protection.classic_unreadable", repo=repo, base=base, error=str(exc))
        return _Reading(False, [], UNKNOWN)
    except Exception as exc:  # nosec B110 - advisory probe; the fallback is "gate on all"
        log.info("protection.classic_unreadable", repo=repo, base=base, error=str(exc))
        return _Reading(False, [], UNKNOWN)
    if protection is None:
        return _Reading(True, [], UNKNOWN)  # explicitly unprotected — an answer (#558)
    if not isinstance(protection, dict):
        return _Reading(True, [], UNKNOWN)
    reviews = protection.get("required_pull_request_reviews")
    reviews = reviews if isinstance(reviews, dict) else {}
    return _Reading(
        True,
        _classic_contexts(protection),
        BaseRequirements(
            None,
            _count(reviews, "required_approving_review_count"),
            "protection",
            code_owner_review=_flag(reviews, "require_code_owner_reviews"),
            last_push_approval=_flag(reviews, "require_last_push_approval"),
            dismiss_stale_reviews=_flag(reviews, "dismiss_stale_reviews"),
            conversation_resolution=_enabled(protection, "required_conversation_resolution"),
            linear_history=_enabled(protection, "required_linear_history"),
            signed_commits=_enabled(protection, "required_signatures"),
        ),
    )


def _read_rulesets(ops: Any, repo: str, base: str) -> _Reading:
    try:
        rules = ops.raw("GET", f"/repos/{repo}/rules/branches/{base}")
    except Exception as exc:  # nosec B110 - advisory probe; the fallback is "gate on all"
        log.info("protection.rulesets_unreadable", repo=repo, base=base, error=str(exc))
        return _Reading(False, [], UNKNOWN)
    if not isinstance(rules, list):
        return _Reading(False, [], UNKNOWN)
    return _Reading(True, *_ruleset_requirements(rules))


def _classic_contexts(protection: dict[str, Any]) -> list[str]:
    """The required status checks of a classic protection payload: the
    legacy ``contexts`` list, or the newer ``checks[].context`` — GitHub
    serves both, populated from the same setting."""
    block = protection.get("required_status_checks")
    if not isinstance(block, dict):
        return []
    out: list[str] = []
    contexts = block.get("contexts")
    if isinstance(contexts, list):
        out.extend(str(c) for c in contexts if c)
    checks = block.get("checks")
    if isinstance(checks, list):
        out.extend(
            str(c.get("context")) for c in checks if isinstance(c, dict) and c.get("context")
        )
    return list(dict.fromkeys(out))


def _ruleset_requirements(rules: list[Any]) -> tuple[list[str], BaseRequirements]:
    """Required contexts and every other requirement across the ruleset
    rules that apply to the branch (#673)."""
    contexts: list[str] = []
    approvals = 0
    flags: dict[str, Any] = {}
    deployments: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        params = rule.get("parameters")
        params = params if isinstance(params, dict) else {}
        kind = rule.get("type")
        if kind == "required_status_checks":
            required = params.get("required_status_checks")
            if isinstance(required, list):
                contexts.extend(
                    str(c.get("context"))
                    for c in required
                    if isinstance(c, dict) and c.get("context")
                )
        elif kind == "pull_request":
            approvals = max(approvals, _count(params, "required_approving_review_count"))
            for field, key in (
                ("code_owner_review", "require_code_owner_review"),
                ("last_push_approval", "require_last_push_approval"),
                ("dismiss_stale_reviews", "dismiss_stale_reviews_on_push"),
                ("conversation_resolution", "required_review_thread_resolution"),
            ):
                flags[field] = flags.get(field, False) or _flag(params, key)
        elif kind == "required_linear_history":
            flags["linear_history"] = True
        elif kind == "required_signatures":
            flags["signed_commits"] = True
        elif kind == "merge_queue":
            flags["merge_queue"] = True
        elif kind == "required_deployments":
            envs = params.get("required_deployment_environments")
            if isinstance(envs, list):
                deployments.extend(str(e) for e in envs if e)
    return list(dict.fromkeys(contexts)), BaseRequirements(
        None,
        approvals,
        "rulesets",
        required_deployments=tuple(dict.fromkeys(deployments)),
        **flags,
    )


def _count(block: Any, key: str) -> int:
    """``block[key]`` as a non-negative count; 0 when absent or malformed."""
    if not isinstance(block, dict):
        return 0
    try:
        return max(int(block.get(key) or 0), 0)
    except (TypeError, ValueError):
        return 0


def _flag(block: Any, key: str) -> bool:
    return isinstance(block, dict) and block.get(key) is True


def _enabled(protection: dict[str, Any], key: str) -> bool:
    """A classic ``{"enabled": true}`` block."""
    return _flag(protection.get(key), "enabled")
