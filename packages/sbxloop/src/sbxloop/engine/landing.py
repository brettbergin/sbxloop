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

import functools
import time
from collections.abc import Callable, Container, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from sbxloop.config import MERGE_METHOD_ORDER, LandingConfig, MergeMethod
from sbxloop.engine.checks import (
    NO_POLICY,
    CheckJudgment,
    CheckPolicy,
    PolicyFor,
    judge_checks,
    merged_over_comment,
    no_policy,
)
from sbxloop.engine.model import FixKind
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import (
    FailedCheck,
    GithubOps,
    Identity,
    PaginationError,
    ReviewThread,
    fold_reviews,
    identities_match,
    is_bot_user,
    logins_match,
    raw_pages,
    user_identity,
    user_kind,
)
from sbxloop.gh.protection import BaseRequirements
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

# Human threads acknowledged in one landing pass, at most (#613). A pass
# that hits the cap says so and the gate blocks on the rest — a human who
# opened that many threads is a human who is looking at the PR.
ACK_CAP = 25

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
    # The base's rules the loop cannot satisfy, one per rule (#673) —
    # empty when the block is not about the base's rules, or they could
    # not be read.
    blockers: tuple[str, ...] = ()


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
    # The reviewer is a GitHub App (REST ``user.type == "Bot"``, #613).
    is_bot: bool = False


class NeedsFix(NamedTuple):
    kind: FixKind
    why: str
    failed_checks: tuple[FailedCheck, ...] = ()
    objections: str = ""
    human: tuple[HumanObjection, ...] = ()
    # The judgment behind a ``ci`` fix (#611): what gates, what the base
    # already had red, and which advisory regressions this round is for.
    checks: CheckJudgment | None = None


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
    policy: CheckPolicy = NO_POLICY,
) -> CheckJudgment:
    """Wait for the checks on ``head_sha`` to reach a final state, judged
    under ``policy`` (:mod:`sbxloop.engine.checks`, #611).

    ``red`` returns immediately — the build is known broken and waiting on
    stragglers only delays the fix. ``green`` with check runs present
    returns immediately too. ``green`` with *no* check runs is trusted only
    once ``ci_settle_s`` has passed since ``settle_from`` (the delivery, by
    default now): Actions registers its check runs seconds after a push,
    and reading "nothing failed yet" as success would merge before CI
    started. Past ``ci_timeout_s`` raises :class:`CiTimeout`. Only gating
    checks are waited on: an advisory check still running when every
    gating one is green does not hold the landing.

    ``emit`` fires only when the judgment changes, so a long wait costs
    one event, not one per poll.
    """
    started = clock()
    settle_from = started if settle_from is None else settle_from
    last: CheckJudgment | None = None
    while True:
        verdict = ops.pr_checks(repo, head_sha)
        checks = judge_checks(verdict, policy)
        if checks != last:
            emit(
                state=checks.state,
                total=verdict.total,
                pending=list(checks.pending),
                failed=list(verdict.failed),
                head_sha=head_sha,
                waited_s=round(clock() - started),
                **{k: v for k, v in checks.event().items() if k not in ("state", "pending")},
            )
            last = checks
        if checks.state == "red" or checks.needs_approval:
            # Red: known broken, nothing to wait for. Unapproved: a
            # maintainer has to click before anything runs (#612), and no
            # amount of polling is that click.
            return checks
        if checks.state == "green" and (
            verdict.total > 0 or clock() - settle_from >= cfg.ci_settle_s
        ):
            return checks
        if clock() - started >= cfg.ci_timeout_s:
            raise CiTimeout(
                f"CI did not report within ci_timeout_s={cfg.ci_timeout_s:g}s: {checks.summary()}"
            )
        tick("ci")


class LoopIdentity(NamedTuple):
    """The loop's own GitHub account: its login and, when the source that
    answered says so, whether it is an App (#622). ``login == ""`` is the
    unknown identity, which landing refuses to classify threads with."""

    login: str
    is_bot: bool | None = None

    @property
    def identity(self) -> Identity:
        return self.login, self.is_bot


UNKNOWN_IDENTITY = LoopIdentity("")


def resolve_identity(
    ops: GithubOps,
    repo: str,
    pr_number: int | None,
    *,
    bot_login: str | None = None,
    configured_login: str | None = None,
    pr_author_is_loop: bool = True,
) -> LoopIdentity:
    """The loop's own GitHub identity, from whatever source can answer.

    Resolution order: the App's ``<slug>[bot]`` when the host resolved one
    (an installation token cannot call ``GET /user`` — 403, #581; kind
    App); ``GET /user`` (PAT mode; kind from the payload's ``type``);
    ``[github] bot_login`` when the operator set it (an App when spelt
    ``<slug>[bot]`` — no user login carries brackets — else kind
    unknown); the author of the
    delivered PR, **only** when ``pr_author_is_loop`` — the review
    credential is the delivery credential, so the same token opened it
    (#622: a reviewer-only identity would otherwise adopt the deliverer's
    name and misread its own threads as a human's); ``""`` — which landing
    then refuses to classify threads with (:func:`_reconciliation_block`).
    The engine caches the answer per drive (``LoopEngine._login``); the
    daemon's gate-approve path calls it fresh.
    """
    if bot_login:
        return LoopIdentity(bot_login, True)
    try:
        user = ops.raw("GET", "/user")
        login = str(user.get("login", "")) if isinstance(user, dict) else ""
        if login:
            return LoopIdentity(login, user_kind(user))
    except GithubOpsError as exc:
        log.warning(
            "land.login_lookup_failed",
            repo=repo,
            pr=pr_number,
            http_status=exc.http_status,
            error=str(exc),
            hint="GET /user needs a user token — a GitHub App installation "
            "token cannot call it; falling back to [github] bot_login, then "
            "to the delivered PR's author",
        )
    if configured_login:
        return LoopIdentity(configured_login, True if configured_login.endswith("[bot]") else None)
    if pr_number is None or not pr_author_is_loop:
        return UNKNOWN_IDENTITY
    try:
        user = ops.pr_get(repo, pr_number).get("user")
    except GithubOpsError:
        log.warning("land.login_pr_author_lookup_failed", repo=repo, pr=pr_number, exc_info=True)
        return UNKNOWN_IDENTITY
    login, kind = user_identity(user)
    return LoopIdentity(login, kind) if login else UNKNOWN_IDENTITY


def resolve_login(
    ops: GithubOps,
    repo: str,
    pr_number: int | None,
    *,
    bot_login: str | None = None,
    configured_login: str | None = None,
    pr_author_is_loop: bool = True,
) -> str:
    """:func:`resolve_identity`'s login alone."""
    return resolve_identity(
        ops,
        repo,
        pr_number,
        bot_login=bot_login,
        configured_login=configured_login,
        pr_author_is_loop=pr_author_is_loop,
    ).login


def human_objection(
    ops: GithubOps, repo: str, number: int, *, login: str, is_bot: bool | None = None
) -> bool:
    """Whether a reviewer other than the loop's own identity has a standing
    ``CHANGES_REQUESTED`` on the PR. The loop's own reviews are excluded
    rather than trusted: our verdict lives in the run, not on GitHub."""
    payload = raw_pages(ops, f"/repos/{repo}/pulls/{number}/reviews")
    others = [
        review
        for review in payload
        if isinstance(review, dict)
        and not identities_match(user_identity(review.get("user")), (login, is_bot))
    ]
    return fold_reviews(others) == "CHANGES_REQUESTED"


def human_objections(
    ops: GithubOps, repo: str, number: int, *, login: str, is_bot: bool | None = None
) -> list[HumanObjection]:
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

    def bot(entry: dict[str, Any]) -> bool:
        return is_bot_user(entry.get("user"))

    payload = raw_pages(ops, f"/repos/{repo}/pulls/{number}/reviews")
    latest: dict[str, dict[str, Any]] = {}
    for review in payload:
        if not isinstance(review, dict):
            continue
        who = login_of(review)
        if identities_match(user_identity(review.get("user")), (login, is_bot)):
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
                is_bot=bot(review),
            )
        )
    comments = raw_pages(ops, f"/repos/{repo}/pulls/{number}/comments")
    for comment in comments:
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
                is_bot=bot(comment) or bot(latest[who]),
            )
        )
    return out


def automated(login: str, is_bot: bool, cfg: LandingConfig) -> bool:
    """Whether a reviewer is a machine (#613): a GitHub App, or a User-type
    account the operator listed in ``[landing] ignore_reviewers`` (a bot
    that reviews from a personal token). There is no reverse list — an
    App is never a human."""
    return is_bot or any(logins_match(login, name) for name in cfg.ignore_reviewers)


def unreconciled_threads(
    threads: Sequence[ReviewThread],
    *,
    login: str,
    ignore: Sequence[str] = (),
    is_bot: bool | None = None,
) -> tuple[list[str], list[str]]:
    """Split the PR's inline threads into the loop's own and the humans'
    that are *not* reconciled yet, as anchors ready to name in a reason.

    A **loop-authored** thread (its root comment is the loop's) is reconciled
    when it is resolved — the addressed case — or carries a later reply from
    the loop, which is the refuted case: the loop said why it disagrees and
    deliberately left the thread open.

    A **human** thread is reconciled when the loop replied in it. It is
    never required to be resolved: closing a human's thread is theirs to do.

    Threads with no comments at all cannot be spoken to and are ignored. So
    is a thread a bot opened (a GitHub App, or a login in ``ignore`` —
    ``[landing] ignore_reviewers``): a bot's inline comments reach the loop
    through the one fix round its changes-requested review buys (#613),
    never through the reconciliation gate, which is for people.

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
        # GraphQL spells an App's login without the [bot] suffix REST uses;
        # the fold in identities_match reconciles the two (the r9t8hnv33
        # field failure) and the kind, when both sides know it, keeps a
        # human `foo` apart from an App `foo[bot]` (#622).
        if identities_match(thread.comments[0].identity, (login, is_bot)):
            if thread.is_resolved or thread.has_reply_from(login, is_bot):
                continue
            loop_open.append(thread.anchor)
        elif _bot_thread(thread, ignore):
            continue
        else:
            if thread.has_reply_from(login, is_bot):
                continue
            human_open.append(thread.anchor)
    return loop_open, human_open


def _bot_thread(thread: ReviewThread, ignore: Sequence[str]) -> bool:
    return thread.opened_by_bot or any(
        logins_match(thread.comments[0].login, name) for name in ignore
    )


def _read_threads(
    ops: GithubOps, repo: str, number: int, *, tick: Tick
) -> list[ReviewThread] | Blocked:
    """The PR's inline threads, retried through transient failures.

    :class:`Blocked` when GitHub would not answer in
    :data:`THREAD_READ_ATTEMPTS` attempts — a one-off 502 must not strand
    a run that cleared every other bar, but a persistently unreadable PR
    still blocks: "we could not tell" is not "there is nothing to answer".
    A :class:`PaginationError` is that answer on the first read: the list
    is longer than the loop will follow, and retrying does not shorten it.
    """
    for attempt in range(1, THREAD_READ_ATTEMPTS + 1):
        try:
            return list(ops.pr_review_threads(repo, number))
        except PaginationError as exc:
            log.warning("land.threads_unread", repo=repo, pr=number, error=str(exc))
            return Blocked(f"its review threads were not all read: {exc}")
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
    return Blocked(
        "its review threads could not be read, so reconciliation cannot "
        f"be confirmed (after {THREAD_READ_ATTEMPTS} attempts)"
    )


def _reconciliation_block(
    ops: GithubOps,
    repo: str,
    number: int,
    *,
    cfg: LandingConfig,
    login: str,
    tick: Tick,
    emit: Emit,
    ack: Ack | None = None,
    is_bot: bool | None = None,
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
    answers non-blocking commenters. At most :data:`ACK_CAP` of them per
    pass, and never a bot's thread (#613): the rest, if any, block
    truthfully.
    """
    threads = _read_threads(ops, repo, number, tick=tick)
    if isinstance(threads, Blocked):
        return threads
    if not threads:
        return None
    if not login:
        # With threads present, an unknown identity can neither honour the
        # gate nor safely answer for it: classification would call every
        # loop thread a human's — the App-auth field failure this guard
        # replaces with the truth.
        return Blocked(_IDENTITY_WHY)
    ignore = tuple(cfg.ignore_reviewers)
    loop_open, human_open = unreconciled_threads(threads, login=login, ignore=ignore, is_bot=is_bot)
    capped = 0
    if human_open and ack is not None:
        pending = [
            t
            for t in threads
            if t.comments
            and not identities_match(t.comments[0].identity, (login, is_bot))
            and not _bot_thread(t, ignore)
            and not t.has_reply_from(login, is_bot)
        ]
        capped = max(0, len(pending) - ACK_CAP)
        acked = ack(pending[:ACK_CAP])
        if acked:
            emit("land.human_ack", pr=number, acked=acked)
        if capped:
            emit("land.human_ack_capped", pr=number, acked=acked, remaining=capped, cap=ACK_CAP)
        reread = _read_threads(ops, repo, number, tick=tick)
        if not isinstance(reread, Blocked):
            loop_open, human_open = unreconciled_threads(reread, login=login, ignore=ignore)
    if loop_open:
        return Blocked(f"{len(loop_open)} review threads unreconciled: {', '.join(loop_open)}")
    if human_open:
        note = f" (acknowledgments are capped at {ACK_CAP} per landing pass)" if capped else ""
        return Blocked(
            f"{len(human_open)} human review threads have no reply{note}: {', '.join(human_open)}"
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
    policy_for: PolicyFor = no_policy,
    bot_round_spent: bool = False,
    is_bot: bool | None = None,
    settle_from: float | None = None,
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

    ``policy_for`` judges the head's checks against the base (#611): which
    of them gate, and which reds the base already had. The default judges
    the way the loop always did — every check gates, every red is ours.
    A red the merge goes over is named in a PR comment before the merge.

    ``bot_round_spent`` is whether this run already had its one fix round
    for an automated reviewer's changes-requested review (#613). A bot's
    objection buys exactly that round; afterwards its standing review is
    merged over and named — bots do not dismiss, and a human's authority
    is the only kind that blocks.

    ``is_bot`` is the loop's own kind when known (#622), so its threads
    and reviews are told from a same-named account of the other kind.

    A head with **no checks at all** is not trusted on sight (#633): the
    read is handed to :func:`poll_checks`, which waits out ``ci_settle_s``
    from the moment this head was first seen here before "no CI" means
    no CI. A resume at landing and the merge-gate approve path enter
    here with no poll before them, and an update-branch makes a head no
    poll has seen — either would otherwise merge before a slow CI had
    registered its first run. A caller that already waited on the head it
    enters with passes ``settle_from`` (the CI stage's delivery time) so
    the window is not paid twice.
    """
    started = clock()
    last_checks: CheckJudgment | None = None
    named: set[tuple[str, ...]] = set()
    bots_named: set[tuple[str, ...]] = set()
    method: MergeMethod | None = None
    blocked_at: str | None = None
    head_seen: tuple[str, float] | None = None
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
        if head_seen is None or head_seen[0] != head:
            # The entry head's clock may come from the caller; a head
            # that turns up later (update-branch) starts its own.
            since = clock() if head_seen is not None or settle_from is None else settle_from
            head_seen = (head, since)
        standing = human_objections(ops, repo, number, login=login, is_bot=is_bot)
        if standing and not login:
            # An unknown identity cannot exclude the loop's own reviews
            # (landing.py's `login` filter), so these "objections" may be
            # our own words: a fix round on them is budget burn, not
            # autonomy. Hand over with the truth instead.
            return Blocked(_IDENTITY_WHY)
        bots = [o for o in standing if automated(o.login, o.is_bot, cfg)]
        standing = [o for o in standing if not automated(o.login, o.is_bot, cfg)]
        unanswered = [o for o in standing if o.key not in answered]
        if standing and unanswered:
            return NeedsFix(
                "human",
                "a reviewer requested changes on the pull request",
                objections=ops.pr_review_feedback(
                    repo, number, exclude_login=login, exclude_is_bot=is_bot
                ),
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
        if bots and not bot_round_spent:
            # An automated reviewer's objection is a signal worth one round
            # (#613): its findings go into the brief and its threads get
            # their reply from the reconciliation that follows the fix.
            # It is not an authority — a bot never dismisses its review.
            return NeedsFix(
                "bot",
                "an automated reviewer requested changes on the pull request",
                objections=ops.pr_review_feedback(
                    repo, number, exclude_login=login, exclude_is_bot=is_bot
                ),
                human=tuple(o for o in bots if o.key not in answered),
            )
        bot_reviewers = tuple(dict.fromkeys(o.login for o in bots))
        if bot_reviewers and bot_reviewers not in bots_named:
            bots_named.add(bot_reviewers)
            emit("land.bot_standing", pr=number, reviewers=list(bot_reviewers))
        policy = policy_for(head)
        checks = judge_checks(ops.pr_checks(repo, head), policy)
        if (
            checks.state == "green"
            and checks.verdict.total == 0
            and clock() - head_seen[1] < cfg.ci_settle_s
        ):
            # Nothing has reported on this head — neither a check run nor
            # a status. Settle before believing it (#633): the same window
            # the CI stage applies after a delivery, counted from when
            # this head was first seen here. Past the window the read
            # stands: no CI is no CI.
            try:
                checks = poll_checks(
                    ops,
                    repo,
                    head,
                    cfg=cfg,
                    tick=tick,
                    emit=functools.partial(emit, "landing.checks", pr=number, head=head),
                    clock=clock,
                    settle_from=head_seen[1],
                    policy=policy,
                )
            except CiTimeout as exc:
                return Blocked(str(exc))
            if checks.verdict.total > 0:
                # CI turned up during the settle; the outcome is judged
                # below, and a red one gets its round.
                emit("land.settled", pr=number, head=head, checks=checks.verdict.total)
        if checks.needs_approval and checks.state != "red":
            # A workflow a maintainer has not approved (#612): no commit
            # fixes it and no wait ends it. A real red still comes first —
            # that round is worth spending whatever else waits.
            emit("landing.checks", pr=number, head=head, **checks.event())
            return Blocked(checks.summary())
        if checks.state == "pending":
            tick("ci")
            continue
        if checks.noteworthy and checks != last_checks:
            emit("landing.checks", pr=number, head=head, **checks.event())
            last_checks = checks
        if checks.state == "red":
            return NeedsFix(
                "ci",
                checks.summary(),
                failed_checks=tuple(
                    c for c in ops.checks_failed_logs(repo, head) if c.name in checks.fix
                ),
                checks=checks,
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
        if str(pr.get("mergeable_state") or "") == "blocked":
            # Every check the loop judges is green, yet GitHub says the PR
            # may not merge: the base's rules want something no round
            # supplies (#620). Name it rather than PUT and parse a 405 —
            # on the second read: the PR was read before its checks were,
            # and a state computed while they were still pending is stale.
            # `unstable` (a non-required check red) is mergeable and is
            # merged over, consistent with the baseline judgment (#611).
            if blocked_at != head:
                blocked_at = head
                tick("mergeability")
                continue
            return blocked_by_base(policy.requirements, cfg, can_sign=bool(is_bot))
        # #520 step 5: the last gates before the merge are about the review
        # record itself. A run that could not post its approving review has
        # no review on the PR at all (#503), and a PR whose findings are
        # still open on their threads has not been reconciled — neither may
        # merge silently.
        if not review_posted:
            return Blocked("review record could not be posted")
        blocked = _reconciliation_block(
            ops,
            repo,
            number,
            cfg=cfg,
            login=login,
            tick=tick,
            emit=emit,
            ack=ack,
            is_bot=is_bot,
        )
        if blocked is not None:
            emit("land.unreconciled", pr=number, why=blocked.why)
            return blocked
        if gate:
            # The one permissible human gate, and only ever here: everything
            # a human could be waiting on is already settled.
            emit("land.gated", pr=number, head=head)
            return Gated(head)
        comment = merged_over_comment(checks)
        if comment is not None and checks.merged_over not in named:
            # Said before the merge, on the PR, so the red the loop went
            # over is on the record where the next reader looks — best
            # effort: a comment refusal must not stop a merge every bar
            # has cleared.
            named.add(checks.merged_over)
            _say(ops, repo, number, comment)
        if bot_reviewers and ("comment", *bot_reviewers) not in bots_named:
            bots_named.add(("comment", *bot_reviewers))
            _say(ops, repo, number, bot_review_comment(bot_reviewers))
        if method is None:
            # Resolved once per landing, last of all (#620): an explicit
            # method the repository refuses is a block, never a substitute.
            allowed = allowed_merge_methods(_repo_payload(ops, repo))
            method, why = resolve_merge_method(cfg.merge_method, allowed)
            if method is None:
                return Blocked(why)
            emit("land.merge_method", pr=number, method=method, configured=cfg.merge_method)
        outcome = ops.pr_merge(repo, number, method=method, sha=head)
        if outcome.stale:
            # The head moved between the read that judged it and the merge;
            # the next iteration judges the new head.
            log.info("land.merge_stale", repo=repo, pr=number, detail=outcome.reason)
            tick("merge")
            continue
        if outcome.blocked:
            # A bare 405 says "not mergeable" and nothing more. The base's
            # rules say what (#620); GitHub's own words ride along.
            return blocked_by_base(
                policy.requirements, cfg, detail=outcome.reason, can_sign=bool(is_bot)
            )
        if cfg.delete_branch_on_merge and branch:
            try:
                ops.branch_delete(repo, branch)
            except GithubOpsError:
                # The merge already happened; a leftover branch is untidy,
                # not a failure of the thing that just succeeded.
                log.warning("land.branch_delete_failed", repo=repo, branch=branch, exc_info=True)
        return Landed(outcome.sha)


# The repository payload's flag for each merge method (GitHub's names).
MERGE_METHOD_FLAGS: dict[MergeMethod, str] = {
    "squash": "allow_squash_merge",
    "merge": "allow_merge_commit",
    "rebase": "allow_rebase_merge",
}


def allowed_merge_methods(repo: dict[str, Any] | None) -> tuple[MergeMethod, ...] | None:
    """The merge methods a repository payload allows, in the loop's order
    of preference, or None when the payload does not say (#620) — a
    partial payload, or a read that failed."""
    if not isinstance(repo, dict):
        return None
    flags = {m: repo.get(MERGE_METHOD_FLAGS[m]) for m in MERGE_METHOD_ORDER}
    if any(v is None for v in flags.values()):
        return None
    return tuple(m for m in MERGE_METHOD_ORDER if flags[m])


def resolve_merge_method(
    configured: MergeMethod, allowed: tuple[MergeMethod, ...] | None
) -> tuple[MergeMethod | None, str]:
    """The method to merge with, or None and why not (#620).

    ``auto`` takes the first of squash → merge → rebase the repository
    allows; when the repository could not be read it takes squash and
    lets the merge answer. An explicit method the repository disallows
    is a block, never a quiet substitute: the operator asked for a
    history shape, and the loop must not write a different one.
    """
    if configured == "auto":
        if allowed is None:
            return "squash", "the repository's merge settings could not be read; trying squash"
        if not allowed:
            return None, (
                "the repository allows no merge method at all (allow_squash_merge, "
                "allow_merge_commit and allow_rebase_merge are all off); a merge queue "
                "or a human has to land it"
            )
        return allowed[0], f"first allowed of {', '.join(MERGE_METHOD_ORDER)}"
    if allowed is not None and configured not in allowed:
        options = ", ".join(allowed) if allowed else "none"
        return None, (
            f'`[landing] merge_method = "{configured}"` is not allowed by the repository '
            f"(it allows: {options}); change the setting or the repository's merge "
            "options — the loop will not merge a different way than configured"
        )
    return configured, "as configured"


def _repo_payload(ops: GithubOps, repo: str) -> dict[str, Any] | None:
    try:
        return ops.repo_get(repo)
    except GithubOpsError:
        log.warning("land.repo_unread", repo=repo, exc_info=True)
        return None


def blocked_by_base(
    requirements: BaseRequirements,
    cfg: LandingConfig,
    *,
    detail: str = "",
    can_sign: bool = False,
) -> Blocked:
    """The outcome for a PR GitHub will not merge with its checks green:
    :func:`blocked_reason` as the run's verdict, and the base's blockers
    (#673) for the event and the transcript."""
    blockers = base_blockers(requirements, cfg, can_sign=can_sign)
    return Blocked(blocked_reason(requirements, cfg, detail=detail, can_sign=can_sign), blockers)


def base_blockers(
    requirements: BaseRequirements, cfg: LandingConfig, *, can_sign: bool = False
) -> tuple[str, ...]:
    """The base's rules the loop as configured cannot satisfy (#673)."""
    return tuple(
        requirements.blockers(can_approve=False, can_sign=can_sign, merge_method=cfg.merge_method)
    )


def blocked_reason(
    requirements: BaseRequirements,
    cfg: LandingConfig,
    *,
    detail: str = "",
    can_sign: bool = False,
) -> str:
    """Why GitHub will not merge a PR whose checks are green (#620): every
    rule of the base the loop is known not to satisfy, one per line
    (#673); else the usual suspects — and the one knob that helps when a
    review is the bar."""
    blockers = base_blockers(requirements, cfg, can_sign=can_sign)
    if blockers:
        why = "the base branch's rules require what the loop cannot supply:\n" + "\n".join(
            f"- {reason}" for reason in blockers
        )
        if requirements.approvals_required and cfg.merge_gate != "chat":
            why += '\n- set `[landing] merge_gate = "chat"` to have a person approve from chat'
    elif requirements.source == "unknown":
        why = (
            "GitHub reports the pull request as blocked with every check green, and the "
            "base's protection could not be read; the usual causes are a required "
            "review, a CODEOWNERS review, required conversation resolution, or a merge "
            "queue"
        )
    else:
        why = (
            "GitHub reports the pull request as blocked with every check green; the "
            "base's rules require something the loop cannot supply — a rule it does not "
            "read, or a check it does not see"
        )
    if detail:
        why += f" (GitHub: {detail})"
    return why


def bot_review_comment(reviewers: Sequence[str]) -> str:
    """The PR comment naming the automated reviewers whose changes-requested
    review the merge goes over (#613)."""
    who = ", ".join(f"`{name}`" for name in reviewers)
    return (
        f"Merged with a changes-requested review from an automated reviewer still "
        f"standing: {who}. Its findings had one fix round and were answered on "
        "their threads; bots do not dismiss their reviews, and only a person's "
        "review blocks a merge."
    )


def _say(ops: GithubOps, repo: str, number: int, body: str) -> None:
    """A best-effort PR comment: a refusal must not stop a merge every bar
    has cleared."""
    try:
        ops.pr_issue_comment(repo, number, body)
    except GithubOpsError:
        log.warning("land.comment_unposted", repo=repo, pr=number, exc_info=True)


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
