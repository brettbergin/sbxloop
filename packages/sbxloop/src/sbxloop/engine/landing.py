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
from collections.abc import Callable, Container, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from sbxloop.config import LandingConfig
from sbxloop.engine.model import FixKind
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import ChecksVerdict, FailedCheck, GithubOps, ReviewThread, fold_reviews
from sbxloop.log import get_logger

log = get_logger(__name__)

# What the engine is waiting on when it calls `tick`; rendered by surfaces.
Waiting = str
Tick = Callable[[Waiting], None]
Emit = Callable[..., None]

# How the landing-time acknowledgement of human threads is injected: the
# engine binds run identity and ops; landing hands over the threads that
# still lack a loop reply and learns how many replies landed.
Ack = Callable[[Sequence[ReviewThread]], int]

# A thread listing that fails is retried this many times (one `tick`
# apart) before the gate gives up; a code constant, not a config key —
# there is nothing for an operator to tune about a transient 5xx.
THREAD_READ_ATTEMPTS = 3

_IDENTITY_WHY = (
    "the loop's own GitHub identity could not be resolved, so its review "
    "threads cannot be told from a human's"
)


class Landed(NamedTuple):
    sha: str
    by_human: bool = False


class Gated(NamedTuple):
    """The opt-in merge gate (``[landing] merge_gate``): every bar cleared,
    the merge withheld for one human approval. ``head`` is the sha the
    loop judged — a record, not a contract: the approve path re-runs
    ``land()`` and re-judges the live PR."""

    head: str


class Blocked(NamedTuple):
    why: str


class HumanObjection(NamedTuple):
    """One standing objection from a human reviewer, as something the loop
    can answer *in place* rather than only in a build report.

    ``key`` is the stable identity the store records a reply under, so a
    resumed run — and the next landing pass — knows this objection has
    already been answered. Inline objections carry a thread; a review body
    objection does not (``comment_id is None``) and is answered with a PR
    comment instead.
    """

    key: str
    login: str
    body: str
    anchor: str = ""
    comment_id: int | None = None
    thread_node_id: str | None = None


class NeedsFix(NamedTuple):
    kind: FixKind
    why: str
    failed_checks: tuple[FailedCheck, ...] = ()
    objections: str = ""
    human: tuple[HumanObjection, ...] = ()


class Closed(NamedTuple):
    why: str


LandingOutcome = Landed | Gated | Blocked | NeedsFix | Closed


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


def resolve_login(
    ops: GithubOps, repo: str, pr_number: int | None, *, bot_login: str | None = None
) -> str:
    """The loop's own GitHub login, from whatever source can answer.

    Resolution order: the App's ``<slug>[bot]`` when the host resolved one
    (an installation token cannot call ``GET /user`` — 403, #581); ``GET
    /user`` (PAT mode); the author of the delivered PR (the same token
    opened it); ``""`` — which landing then refuses to classify threads
    with (:func:`_reconciliation_block`). The engine caches the answer per
    drive (``LoopEngine._login``); the daemon's gate-approve path calls it
    fresh.
    """
    if bot_login:
        return bot_login
    try:
        user = ops.raw("GET", "/user")
        return str(user.get("login", "")) if isinstance(user, dict) else ""
    except GithubOpsError as exc:
        log.warning(
            "land.login_lookup_failed",
            repo=repo,
            pr=pr_number,
            http_status=exc.http_status,
            error=str(exc),
            hint="GET /user needs a user token — a GitHub App installation "
            "token cannot call it; falling back to the delivered PR's author",
        )
    if pr_number is None:
        return ""
    try:
        user = ops.pr_get(repo, pr_number).get("user")
    except GithubOpsError:
        log.warning("land.login_pr_author_lookup_failed", repo=repo, pr=pr_number, exc_info=True)
        return ""
    return str(user.get("login") or "") if isinstance(user, dict) else ""


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


def human_objections(ops: GithubOps, repo: str, number: int, *, login: str) -> list[HumanObjection]:
    """The standing human objections on a PR, one entry per thing to answer.

    A reviewer's ``CHANGES_REQUESTED`` body is one objection; each of that
    reviewer's inline comments is another, carrying the comment id the loop
    replies on. Only reviewers whose *latest* verdict is
    ``CHANGES_REQUESTED`` count, matching :func:`human_objection` — an
    objection a later approval cleared is not standing any more.

    Returns ``[]`` when nothing stands, which is not the same as a standing
    objection with no readable text: that yields the review entry with an
    empty body, so the loop still has something to answer.
    """

    def login_of(entry: dict[str, Any]) -> str:
        return str((entry.get("user") or {}).get("login") or "")

    payload = ops.raw("GET", f"/repos/{repo}/pulls/{number}/reviews")
    latest: dict[str, dict[str, Any]] = {}
    for review in payload if isinstance(payload, list) else []:
        if not isinstance(review, dict):
            continue
        who = login_of(review)
        if who == login:
            continue
        state = str(review.get("state") or "").upper()
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[who] = review
    objectors = {
        who
        for who, review in latest.items()
        if str(review.get("state") or "").upper() == "CHANGES_REQUESTED"
    }
    if not objectors:
        return []
    out: list[HumanObjection] = []
    for who in sorted(objectors):
        review = latest[who]
        out.append(
            HumanObjection(
                key=f"human:review:{review.get('id')}",
                login=who,
                body=str(review.get("body") or "").strip(),
            )
        )
    comments = ops.raw("GET", f"/repos/{repo}/pulls/{number}/comments")
    for comment in comments if isinstance(comments, list) else []:
        if not isinstance(comment, dict):
            continue
        who = login_of(comment)
        if who not in objectors:
            continue
        body = str(comment.get("body") or "").strip()
        if not body:
            continue
        path = str(comment.get("path") or "")
        line = comment.get("line") or comment.get("original_line")
        anchor = f"{path}:{line}" if path and line else path
        comment_id = comment.get("id")
        out.append(
            HumanObjection(
                key=f"human:comment:{comment_id}",
                login=who,
                body=body,
                anchor=anchor,
                comment_id=int(comment_id) if comment_id is not None else None,
            )
        )
    return out


def unreconciled_threads(
    threads: Sequence[ReviewThread], *, login: str
) -> tuple[list[str], list[str]]:
    """Split the PR's inline threads into the loop's own and the humans'
    that are *not* reconciled yet, as anchors ready to name in a reason.

    A **loop-authored** thread (its root comment is the loop's) is reconciled
    when it is resolved — the addressed case — or carries a later reply from
    the loop, which is the refuted case: the loop said why it disagrees and
    deliberately left the thread open.

    A **human** thread is reconciled when the loop replied in it. It is
    never required to be resolved: closing a human's thread is theirs to do.

    Threads with no comments at all cannot be spoken to and are ignored.

    ``login`` must be the loop's real identity. An empty login would
    classify every loop thread as a human's (and ``has_reply_from("")``
    can never match), which is exactly the field failure that stranded
    App-auth runs — so it is refused rather than guessed at.
    """
    if not login:
        raise ValueError("cannot classify review threads without the loop's own login")
    loop_open: list[str] = []
    human_open: list[str] = []
    for thread in threads:
        if not thread.comments:
            continue
        author = thread.comments[0].login
        if author == login:
            if thread.is_resolved or thread.has_reply_from(login):
                continue
            loop_open.append(thread.anchor)
        else:
            if thread.has_reply_from(login):
                continue
            human_open.append(thread.anchor)
    return loop_open, human_open


def _read_threads(
    ops: GithubOps, repo: str, number: int, *, tick: Tick
) -> list[ReviewThread] | None:
    """The PR's inline threads, retried through transient failures.

    ``None`` when GitHub would not answer in :data:`THREAD_READ_ATTEMPTS`
    attempts — a one-off 502 must not strand a run that cleared every
    other bar, but a persistently unreadable PR still blocks: "we could
    not tell" is not "there is nothing to answer".
    """
    for attempt in range(1, THREAD_READ_ATTEMPTS + 1):
        try:
            return list(ops.pr_review_threads(repo, number))
        except GithubOpsError as exc:
            log.warning(
                "land.threads_unreadable",
                repo=repo,
                pr=number,
                attempt=attempt,
                error=str(exc),
            )
            if attempt < THREAD_READ_ATTEMPTS:
                tick("threads")
    return None


def _reconciliation_block(
    ops: GithubOps,
    repo: str,
    number: int,
    *,
    login: str,
    tick: Tick,
    emit: Emit,
    ack: Ack | None = None,
) -> Blocked | None:
    """The merge gate of #520 step 5: a pull request does not merge while a
    review finding on it is still unanswered.

    Returns ``None`` when every thread is reconciled (including the trivial
    case of a PR with no inline threads at all), a :class:`Blocked` naming
    the offending threads otherwise. Reads are retried
    (:func:`_read_threads`) before "could not be read" blocks; merging on
    an unread PR is precisely the silent merge this gate exists to stop.

    A human thread with no loop reply is first **answered**, not waited
    on: no human asked for this wait, so the loop replies itself (``ack``,
    bound by the engine to
    :func:`sbxloop.engine.reconcile.acknowledge_human_threads`) and judges
    again on a fresh read. A standing changes-requested review never
    reaches here — ``land()`` hands over on it first — so an ack only ever
    answers non-blocking commenters.
    """
    threads = _read_threads(ops, repo, number, tick=tick)
    if threads is None:
        return Blocked(
            "its review threads could not be read, so reconciliation cannot "
            f"be confirmed (after {THREAD_READ_ATTEMPTS} attempts)"
        )
    if not threads:
        return None
    if not login:
        # With threads present, an unknown identity can neither honour the
        # gate nor safely answer for it: classification would call every
        # loop thread a human's — the App-auth field failure this guard
        # replaces with the truth.
        return Blocked(_IDENTITY_WHY)
    loop_open, human_open = unreconciled_threads(threads, login=login)
    if human_open and ack is not None:
        pending = [
            t
            for t in threads
            if t.comments and t.comments[0].login != login and not t.has_reply_from(login)
        ]
        acked = ack(pending)
        if acked:
            emit("land.human_ack", pr=number, acked=acked)
        reread = _read_threads(ops, repo, number, tick=tick)
        if reread is not None:
            loop_open, human_open = unreconciled_threads(reread, login=login)
    if loop_open:
        return Blocked(f"{len(loop_open)} review threads unreconciled: {', '.join(loop_open)}")
    if human_open:
        return Blocked(
            f"{len(human_open)} human review threads have no reply: {', '.join(human_open)}"
        )
    return None


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
    answered: Container[str] = frozenset(),
    review_posted: bool = True,
    ack: Ack | None = None,
    gate: bool = False,
) -> LandingOutcome:
    """Drive the PR to a landing decision, polling until one is reached.

    Returns ``Landed`` (merged — by the loop or, if someone beat it to it,
    by a human), ``Blocked`` (GitHub will not finish it and no round would
    change that), ``NeedsFix`` (something a fix round can change: red CI, a
    conflict with the base, a human's objection) or ``Closed`` (someone
    closed the PR unmerged). ``ci_timeout_s`` bounds the whole wait; a
    landing that has not settled by then is ``Blocked`` too.

    ``review_posted`` is whether the round that approved this PR actually
    got its review onto GitHub. False blocks the merge: a run whose review
    post failed would otherwise merge with no reviewable record at all.

    ``ack`` answers human threads that nothing else in the pipeline would
    ever speak to (see :func:`_reconciliation_block`); ``None`` keeps the
    gate read-only.

    ``gate`` is the opt-in merge gate: True returns :class:`Gated` where
    the merge would have happened — after every other bar — so the caller
    parks the run for one human approval instead of merging.
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
        standing = human_objections(ops, repo, number, login=login)
        if standing and not login:
            # An unknown identity cannot exclude the loop's own reviews
            # (landing.py's `login` filter), so these "objections" may be
            # our own words: a fix round on them is budget burn, not
            # autonomy. Hand over with the truth instead.
            return Blocked(_IDENTITY_WHY)
        unanswered = [o for o in standing if o.key not in answered]
        if standing and unanswered:
            return NeedsFix(
                "human",
                "a reviewer requested changes on the pull request",
                objections=ops.pr_review_feedback(repo, number, exclude_login=login),
                human=tuple(unanswered),
            )
        if standing:
            # Every objection of this standing CHANGES_REQUESTED has already
            # been answered in this run. Only the reviewer can dismiss their
            # own review, so spending another full fix pass on the same
            # words would be pure repeat work (#520): say so and hand over.
            # Doctrine: a human's standing review is a voluntary override
            # the loop respects — not a gate the loop erected. Do not
            # "fix" this into waiting quietly or dismissing their review.
            emit("land.human_answered", pr=number, objections=len(standing))
            return Blocked(
                f"a reviewer's changes-requested review is still standing after "
                f"{len(standing)} replied objection(s); only they can dismiss it"
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
        # #520 step 5: the last gates before the merge are about the review
        # record itself. A run that could not post its approving review has
        # no review on the PR at all (#503), and a PR whose findings are
        # still open on their threads has not been reconciled — neither may
        # merge silently.
        if not review_posted:
            return Blocked("review record could not be posted")
        blocked = _reconciliation_block(
            ops, repo, number, login=login, tick=tick, emit=emit, ack=ack
        )
        if blocked is not None:
            emit("land.unreconciled", pr=number, why=blocked.why)
            return blocked
        if gate:
            # The one permissible human gate, and only ever here: everything
            # a human could be waiting on is already settled.
            emit("land.gated", pr=number, head=head)
            return Gated(head)
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
