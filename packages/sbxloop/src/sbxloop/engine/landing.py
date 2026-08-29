"""Landing a pull request: the CI wait and the merge, as pure decision loops.

Both functions here take a :class:`GithubOps` and a ``tick`` callback and
return a decision; they hold no engine state and run no agent, which is
what makes them unit-testable against a scripted GitHub. The engine owns
what happens *between* polls — draining chat, honouring cancellation,
sleeping — and hands that in as ``tick``.

The gates run in the order of what they cost. The PR's own fate first (a
human merging or closing it outranks everything below), then un-drafting,
then a human's standing objection, then CI (GitHub's compute, free), then
mergeability, and only then the merge itself. A merge sends the head sha
the loop judged, so a push that landed since loses the race with a 409
rather than being merged over.

Two answers come back as data rather than exceptions, and the difference
matters: **405** is GitHub's blanket "not mergeable right now" — most often
a protection rule wanting an approval this identity cannot give — which no
retry fixes, so the run hands over ``Blocked``; **409** is a race, so the
loop simply re-judges the new head.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

from sbxloop.config import LandingConfig
from sbxloop.engine.model import FixKind
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import ChecksVerdict, FailedCheck, GithubOps, fold_reviews
from sbxloop.log import get_logger

log = get_logger(__name__)

# What the engine is waiting on when it calls `tick`; rendered by surfaces.
Waiting = str
Tick = Callable[[Waiting], None]
Emit = Callable[..., None]


class Landed(NamedTuple):
    sha: str
    by_human: bool = False


class Blocked(NamedTuple):
    why: str


class NeedsFix(NamedTuple):
    kind: FixKind
    why: str
    failed_checks: tuple[FailedCheck, ...] = ()
    objections: str = ""


class Closed(NamedTuple):
    why: str


LandingOutcome = Landed | Blocked | NeedsFix | Closed


class CiTimeout(Exception):
    """CI did not report a final state within the wait budget."""


@dataclass
class UpdateState:
    """The update-branch bookkeeping the landing loop mutates; the engine
    persists it through ``on_update`` so a resumed run picks up where the
    budget stood."""

    attempts: int = 0
    # The head an update was requested at. GitHub answers an update with
    # 202 and no sha, so this is how a later poll tells an update still in
    # flight (head unchanged) from one that landed (head moved).
    head: str | None = None


def poll_checks(
    ops: GithubOps,
    repo: str,
    head_sha: str,
    *,
    cfg: LandingConfig,
    tick: Tick,
    emit: Emit,
    clock: Callable[[], float] = time.monotonic,
    settle_from: float | None = None,
) -> ChecksVerdict:
    """Wait for the check runs on ``head_sha`` to reach a final state.

    ``red`` returns immediately — the build is known broken and waiting on
    stragglers only delays the fix. ``green`` with check runs present
    returns immediately too. ``green`` with *no* check runs is trusted only
    once ``ci_settle_s`` has passed since ``settle_from`` (the delivery, by
    default now): Actions registers its check runs seconds after a push,
    and reading "nothing failed yet" as success would merge before CI
    started. Past ``ci_timeout_s`` raises :class:`CiTimeout`.

    ``emit`` fires only when the folded verdict changes, so a long wait
    costs one event, not one per poll.
    """
    started = clock()
    settle_from = started if settle_from is None else settle_from
    last: ChecksVerdict | None = None
    while True:
        verdict = ops.pr_checks(repo, head_sha)
        if verdict != last:
            emit(
                state=verdict.state,
                total=verdict.total,
                pending=list(verdict.pending),
                failed=list(verdict.failed),
                head_sha=head_sha,
                waited_s=round(clock() - started),
            )
            last = verdict
        if verdict.state == "red":
            return verdict
        if verdict.state == "green" and (
            verdict.total > 0 or clock() - settle_from >= cfg.ci_settle_s
        ):
            return verdict
        if clock() - started >= cfg.ci_timeout_s:
            raise CiTimeout(
                f"CI did not report within ci_timeout_s={cfg.ci_timeout_s:g}s: {verdict.summary()}"
            )
        tick("ci")


def human_objection(ops: GithubOps, repo: str, number: int, *, login: str) -> bool:
    """Whether a reviewer other than the loop's own identity has a standing
    ``CHANGES_REQUESTED`` on the PR. The loop's own reviews are excluded
    rather than trusted: our verdict lives in the run, not on GitHub."""
    payload = ops.raw("GET", f"/repos/{repo}/pulls/{number}/reviews")
    if not isinstance(payload, list):
        return False
    others = [
        review
        for review in payload
        if isinstance(review, dict) and str((review.get("user") or {}).get("login") or "") != login
    ]
    return fold_reviews(others) == "CHANGES_REQUESTED"


def land(
    ops: GithubOps,
    repo: str,
    number: int,
    *,
    cfg: LandingConfig,
    branch: str | None,
    node_id: str | None,
    login: str,
    update: UpdateState,
    on_update: Callable[[UpdateState], None],
    tick: Tick,
    emit: Emit,
    clock: Callable[[], float] = time.monotonic,
) -> LandingOutcome:
    """Drive the PR to a landing decision, polling until one is reached.

    Returns ``Landed`` (merged — by the loop or, if someone beat it to it,
    by a human), ``Blocked`` (GitHub will not finish it and no round would
    change that), ``NeedsFix`` (something a fix round can change: red CI, a
    conflict with the base, a human's objection) or ``Closed`` (someone
    closed the PR unmerged). ``ci_timeout_s`` bounds the whole wait; a
    landing that has not settled by then is ``Blocked`` too.
    """
    started = clock()
    while True:
        if clock() - started >= cfg.ci_timeout_s:
            return Blocked(f"landing did not settle within ci_timeout_s={cfg.ci_timeout_s:g}s")
        pr = ops.pr_get(repo, number)
        if pr.get("merged"):
            return Landed(str(pr.get("merge_commit_sha") or ""), by_human=True)
        if str(pr.get("state") or "") == "closed":
            return Closed("the pull request was closed without being merged")
        if pr.get("draft"):
            ready = _undraft(ops, node_id or _str(pr.get("node_id")))
            if not ready:
                return Blocked("its draft status could not be cleared")
            emit("land.undraft", pr=number)
            # GitHub reports a draft's mergeable_state as `draft`; the real
            # merge state only becomes readable on the next read.
            tick("undraft")
            continue
        head = _head_sha(pr)
        if human_objection(ops, repo, number, login=login):
            return NeedsFix(
                "human",
                "a reviewer requested changes on the pull request",
                objections=ops.pr_review_feedback(repo, number, exclude_login=login),
            )
        checks = ops.pr_checks(repo, head)
        if checks.state == "pending":
            tick("ci")
            continue
        if checks.state == "red":
            return NeedsFix(
                "ci", checks.summary(), failed_checks=tuple(ops.checks_failed_logs(repo, head))
            )
        mergeable = pr.get("mergeable")
        if mergeable is None:
            # GitHub computes mergeability asynchronously; "not known yet"
            # is not "mergeable".
            tick("mergeability")
            continue
        if str(pr.get("mergeable_state") or "") == "behind":
            if update.head is not None and update.head == head:
                # An update was requested at this head and the branch has
                # not moved yet — asking again would spend the budget twice.
                tick("update")
                continue
            if update.attempts >= cfg.merge_update_attempts:
                return Blocked(
                    f"still behind its base after {update.attempts} update(s) "
                    f"(merge_update_attempts={cfg.merge_update_attempts})"
                )
            accepted = ops.pr_update_branch(repo, number, expected_head_sha=head)
            update.attempts += 1
            update.head = head if accepted else None
            on_update(update)
            emit("land.update", pr=number, attempt=update.attempts, accepted=accepted)
            tick("update")
            continue
        if not mergeable:
            # A real conflict with the base. A fix round re-delivers, and a
            # re-delivery rebuilds the commit on the current base, so this
            # is genuinely fixable.
            return NeedsFix("conflict", "the pull request conflicts with its base branch")
        outcome = ops.pr_merge(repo, number, method=cfg.merge_method, sha=head)
        if outcome.stale:
            # The head moved between the read that judged it and the merge;
            # the next iteration judges the new head.
            log.info("land.merge_stale", repo=repo, pr=number, detail=outcome.reason)
            tick("merge")
            continue
        if outcome.blocked:
            return Blocked(outcome.reason)
        if cfg.delete_branch_on_merge and branch:
            try:
                ops.branch_delete(repo, branch)
            except GithubOpsError:
                # The merge already happened; a leftover branch is untidy,
                # not a failure of the thing that just succeeded.
                log.warning("land.branch_delete_failed", repo=repo, branch=branch, exc_info=True)
        return Landed(outcome.sha)


def _undraft(ops: GithubOps, node_id: str | None) -> bool:
    if not node_id:
        return False
    try:
        return ops.pr_ready_for_review(node_id)
    except GithubOpsError:
        log.warning("land.undraft_failed", node_id=node_id, exc_info=True)
        return False


def _head_sha(pr: dict[str, Any]) -> str:
    head = pr.get("head")
    sha = head.get("sha") if isinstance(head, dict) else None
    if not sha:
        raise GithubOpsError(f"pull request payload carries no head sha: {pr!r}")
    return str(sha)


def _str(value: object) -> str | None:
    return str(value) if value else None
