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
"""

from __future__ import annotations

from typing import Any, NamedTuple

from sbxloop.errors import GithubOpsError
from sbxloop.log import get_logger

log = get_logger(__name__)


class BaseRequirements(NamedTuple):
    """The merge requirements of one base branch.

    ``required_contexts`` are the check names (check-run ``name`` / status
    ``context``, the shared namespace of #610) the base requires green, or
    ``None`` when a source could not be read — an unknown half can hide a
    requirement, so "some of them" is not an answer. An empty tuple is a
    real answer: both sources read, nothing declared.

    ``requires_reviews`` is whether an approving review is required (the
    one setting incompatible with human-out-of-the-loop operation: the
    loop cannot approve its own pull request). A positive answer from
    either source is conclusive; ``False`` needs both sources read; ``None``
    otherwise.

    ``source`` names where the contexts came from — ``protection``,
    ``rulesets``, ``protection+rulesets``, ``none`` (both read, nothing
    declared) or ``unknown`` — so an event or a doctor row can say so.
    """

    required_contexts: tuple[str, ...] | None
    requires_reviews: bool | None
    source: str


def read_base_requirements(ops: Any, repo: str, base: str) -> BaseRequirements:
    """Read ``base``'s requirements from classic protection and rulesets.

    ``ops`` needs only ``raw(method, path)``. A classic 404 is "explicitly
    unprotected" — an answer; any other failure on either source leaves
    that source unknown. Nothing here raises: the callers are a merge gate
    that must decide something and a doctor that must not crash.
    """
    classic_known = False
    classic_contexts: list[str] = []
    classic_reviews = False
    try:
        protection = ops.raw("GET", f"/repos/{repo}/branches/{base}/protection")
        classic_known = True
        if isinstance(protection, dict):
            classic_contexts = _classic_contexts(protection)
            classic_reviews = _count(
                protection.get("required_pull_request_reviews"), "required_approving_review_count"
            )
    except GithubOpsError as exc:
        if exc.http_status == 404:
            classic_known = True  # explicitly unprotected — an answer, not a failure
        else:
            log.info("protection.classic_unreadable", repo=repo, base=base, error=str(exc))
    except Exception as exc:  # nosec B110 - advisory probe; the fallback is "gate on all"
        log.info("protection.classic_unreadable", repo=repo, base=base, error=str(exc))

    rules_known = False
    rule_contexts: list[str] = []
    rule_reviews = False
    try:
        rules = ops.raw("GET", f"/repos/{repo}/rules/branches/{base}")
        if isinstance(rules, list):
            rules_known = True
            rule_contexts, rule_reviews = _ruleset_requirements(rules)
    except Exception as exc:  # nosec B110 - advisory probe; the fallback is "gate on all"
        log.info("protection.rulesets_unreadable", repo=repo, base=base, error=str(exc))

    if classic_reviews or rule_reviews:
        requires_reviews: bool | None = True
    elif classic_known and rules_known:
        requires_reviews = False
    else:
        requires_reviews = None

    if not (classic_known and rules_known):
        return BaseRequirements(None, requires_reviews, "unknown")
    contexts = tuple(dict.fromkeys([*classic_contexts, *rule_contexts]))
    sources = [
        name
        for name, found in (("protection", classic_contexts), ("rulesets", rule_contexts))
        if found
    ]
    return BaseRequirements(contexts, requires_reviews, "+".join(sources) or "none")


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


def _ruleset_requirements(rules: list[Any]) -> tuple[list[str], bool]:
    """Required contexts and the review requirement across every ruleset
    rule that applies to the branch."""
    contexts: list[str] = []
    reviews = False
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        params = rule.get("parameters")
        kind = rule.get("type")
        if kind == "required_status_checks" and isinstance(params, dict):
            required = params.get("required_status_checks")
            if isinstance(required, list):
                contexts.extend(
                    str(c.get("context"))
                    for c in required
                    if isinstance(c, dict) and c.get("context")
                )
        elif kind == "pull_request" and _count(params, "required_approving_review_count"):
            reviews = True
    return list(dict.fromkeys(contexts)), reviews


def _count(block: Any, key: str) -> bool:
    """Whether ``block[key]`` is a positive count."""
    if not isinstance(block, dict):
        return False
    try:
        return int(block.get(key) or 0) > 0
    except (TypeError, ValueError):
        return False
