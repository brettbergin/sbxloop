"""The shared, forge-neutral types every version-control backend answers in.

What a run reads and writes about a repository — an issue or change
reference, a folded checks verdict, a review thread, the base's merge
requirements — is the same shape whatever forge holds the repository.
These types carry no path, no payload and no transport: a backend under
``vcs/<name>/`` maps its own API into them, and the engine, the daemon and
the doctor read only these.

The spelling still leans on the first backend in places (a ``node_id``, a
``LEFT``/``RIGHT`` side); the roles in :mod:`sbxloop.vcs.protocol` say what
each means, and a second backend translates behind them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, NamedTuple

from pydantic import BaseModel


class IssueRef(BaseModel):
    number: int
    url: str


class PrRef(BaseModel):
    number: int
    url: str


# What a review says about a PR. REQUEST_CHANGES/APPROVE are the ones that
# carry weight: under branch protection they gate the merge, so "the review
# was accepted" becomes a state GitHub enforces rather than one sbxloop only
# tracks. COMMENT is the degraded mode for an identity the repo will not
# accept as a reviewer.
ReviewEvent = Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]

# Folded verdict of a head commit's check runs.
CheckState = Literal["pending", "red", "green"]


class MergeOutcome(NamedTuple):
    """The result of asking GitHub to merge a PR.

    ``blocked`` is the one refusal that is an *answer* rather than an error:
    GitHub says 405 for every "this PR is not mergeable right now" — a draft,
    a failing required check, a protection rule wanting an approval this
    identity cannot give. None of those is fixable by retrying, so the caller
    hands the PR to a human instead of spinning.

    ``stale`` is 409: the head moved between the poll that decided to merge
    and the merge itself. That is a race, not a refusal, and the next poll
    re-decides against the new head.
    """

    merged: bool
    sha: str
    reason: str
    blocked: bool = False
    stale: bool = False


# Where a queued change stands, in the loop's own words: ``queued`` (in
# line), ``testing`` (the queue is running its checks), ``mergeable``
# (checks passed; the queue merges it next), ``blocked`` (the queue will
# not merge it as it is), ``removed`` (taken out of the queue). ``unknown``
# is a state the backend did not recognise — never read as mergeable.
QueueEntryState = Literal["queued", "testing", "mergeable", "blocked", "removed", "unknown"]

# Why an issue is closed: ``completed`` (the work was done) or
# ``not_planned`` (triage — duplicate, won't fix, stale). A backend maps
# these onto whatever its API accepts, or drops the reason when it has none.
CloseReason = Literal["completed", "not_planned"]


class QueueEntry(NamedTuple):
    """The change's place in its base's merge queue (#676): its opaque,
    backend-minted ``id``, its :data:`QueueEntryState`, its position in
    line, and ``head`` — the commit the queue is testing for this entry
    (the queue's own merge-group commit, not the change's head), which is
    where its checks report."""

    id: str
    state: QueueEntryState
    position: int | None = None
    head: str = ""


class QueueState(NamedTuple):
    """What the merge queue has done with the pull request so far (#676):
    whether it is merged or closed, its live entry when it is queued, and
    the queue's removals — ``removals`` counts every removed-from-queue
    event on the PR's timeline (a caller compares against the count it
    saw when it enqueued) and ``removed_reason`` is the latest one's
    reason as GitHub words it."""

    merged: bool
    closed: bool
    entry: QueueEntry | None
    removals: int = 0
    removed_reason: str = ""
    merge_sha: str = ""


class ReviewComment(BaseModel):
    """One inline comment, anchored to a line of the change's diff.

    The anchor is the caller's neutral input — a path, a line, and which
    side of the diff the line is on: ``RIGHT`` the post-change side (the
    default), ``LEFT`` the pre-change side, for a comment on a deleted
    line. A backend resolves it into whatever its API anchors on (a
    commit-and-side pair, a base/start/head position); the vocabulary
    never leaks past the backend."""

    path: str
    line: int
    body: str
    side: Literal["LEFT", "RIGHT"] = "RIGHT"


class PostedFinding(NamedTuple):
    """Where one review finding actually landed on the PR.

    ``anchor`` is the ``path:line`` key the engine carries across rounds.
    ``comment_id`` is the id of the inline comment that anchors the
    finding's thread — ``None`` when the finding was posted in the review
    body instead (anchor refused by the forge, cap overflow, or no line at
    all), which is exactly the case a later reconciliation pass must fall
    back to a plain change-level comment for. ``thread_id`` is the opaque,
    backend-minted id of that comment's review thread, what
    ``resolve_review_thread`` takes; ``None`` when there is no inline
    comment or the lookup could not answer.
    """

    anchor: str
    comment_id: int | None = None
    thread_id: str | None = None


def normalize_login(login: str) -> str:
    """One canonical form for a GitHub identity.

    GraphQL reports an App actor as its bare slug (``sbxloop``) while REST
    attributes the same actor as ``sbxloop[bot]`` — the two spellings must
    compare equal, or the loop misreads its own review threads as a
    human's (field failure r9t8hnv33: fully reconciled PRs ended blocked
    on "human review threads have no reply", with the loop ack-replying to
    its own findings). Logins are case-insensitive on GitHub, so casefold
    too.
    """
    return login.removesuffix("[bot]").casefold()


# A GitHub identity as the loop compares them: the login and whether the
# account is a GitHub App — True (App), False (user), or None when the
# payload it was read from does not say.
Identity = tuple[str, bool | None]


def identities_match(a: Identity, b: Identity) -> bool:
    """Whether two identities are the same account (#622).

    The logins must match under :func:`normalize_login` **and**, when both
    sides know whether they are an App, that must agree: the suffix fold
    makes a human ``foo`` and an App ``foo[bot]`` spell the same, and
    GitHub lets both exist — so the kind, read from the payload
    (``user.type`` on REST, ``author.__typename`` on GraphQL), is what
    tells them apart. Either side not knowing its kind matches on the
    login alone, so nothing regresses where the type is not reported.
    Two empty logins never match: an unknown identity equals nobody.
    """
    (login_a, bot_a), (login_b, bot_b) = a, b
    if not login_a or not login_b or normalize_login(login_a) != normalize_login(login_b):
        return False
    return bot_a is None or bot_b is None or bot_a == bot_b


def logins_match(a: str, b: str) -> bool:
    """Whether two login spellings name the same identity, kinds unknown.
    Two empty logins never match: an unknown identity equals nobody."""
    return identities_match((a, None), (b, None))


class ThreadComment(NamedTuple):
    """One comment inside a review thread."""

    comment_id: int | None
    login: str
    body: str
    # Whether the author is a GitHub App (GraphQL ``author.__typename ==
    # "Bot"``); None when the payload named no type (#622).
    is_bot: bool | None = None

    @property
    def identity(self) -> Identity:
        return self.login, self.is_bot


class ReviewThread(NamedTuple):
    """An inline review thread as it stands on the PR right now.

    Read for idempotency: a reconciliation pass skips a thread that already
    carries its own reply.
    """

    # Opaque and backend-minted: what ``resolve_review_thread`` takes.
    thread_id: str
    is_resolved: bool
    path: str
    line: int | None
    comments: tuple[ThreadComment, ...] = ()

    @property
    def anchor(self) -> str:
        return f"{self.path}:{self.line}" if self.line is not None else self.path

    @property
    def root_comment_id(self) -> int | None:
        return self.comments[0].comment_id if self.comments else None

    @property
    def opened_by_bot(self) -> bool:
        return bool(self.comments) and bool(self.comments[0].is_bot)

    def has_reply_from(self, login: str, is_bot: bool | None = None) -> bool:
        return any(identities_match(c.identity, (login, is_bot)) for c in self.comments[1:])

    def has_reply_marked(self, marker: str, login: str, is_bot: bool | None = None) -> bool:
        """Whether the loop's own reply carrying ``marker`` is on the thread.

        The marker must sit in a comment the loop authored (#618): GitHub's
        quote-reply copies a body verbatim, marker and all, so a human's
        quoted reply would otherwise read as the loop's and the thread —
        and their feedback in it — would be skipped forever.
        """
        return any(
            marker in c.body and identities_match(c.identity, (login, is_bot))
            for c in self.comments[1:]
        )


class SubmittedReview(NamedTuple):
    """A posted review: its url, and the event GitHub actually accepted.

    ``event`` is not necessarily the one requested — see
    :meth:`GithubOps.pr_review_create`.

    ``review_id`` and ``posted`` are the thread identity a later round needs
    to reply on a finding rather than restate it in a fresh review body.
    """

    url: str
    event: ReviewEvent
    review_id: int | None = None
    posted: tuple[PostedFinding, ...] = ()

    @property
    def gates_merge(self) -> bool:
        """Whether this review can hold the merge. A COMMENT cannot."""
        return self.event in ("APPROVE", "REQUEST_CHANGES")

    @property
    def inline(self) -> tuple[PostedFinding, ...]:
        """Findings that got their own thread."""
        return tuple(p for p in self.posted if p.comment_id is not None)

    @property
    def body_only(self) -> tuple[PostedFinding, ...]:
        """Findings that ended up in the review body, with no thread."""
        return tuple(p for p in self.posted if p.comment_id is None)


class ChecksVerdict(NamedTuple):
    """Every check run and commit status on a head commit, folded to one
    answer.

    ``pending`` is deliberately distinct from ``green``: a PR whose checks
    have not reported yet has not passed, and reading "no failures so far"
    as success is exactly how a red PR gets settled as done.

    Check runs (the Checks API — GitHub Actions and most modern apps) and
    commit statuses (the older Status API — Jenkins, Buildkite, Travis,
    CircleCI's default, Codecov, many org bots) are two namespaces GitHub
    keeps separate and the merge box shows together; the verdict merges
    them the same way (#610). Names are the check-run ``name`` or the
    status ``context``, untagged, so a required-context list from branch
    protection (which names both kinds the same way) can be matched
    against them.
    """

    state: CheckState
    total: int
    pending: tuple[str, ...]
    failed: tuple[str, ...]
    # The names that passed, so a required context that has not reported
    # at all can be told from one that reported green (#611).
    passed: tuple[str, ...] = ()
    # Check runs concluded `action_required` (#612): a workflow waiting for
    # a maintainer to approve it — the fork-PR / first-time-contributor
    # gate on GitHub Actions. Not red (no commit fixes it) and not going
    # to finish on its own (waiting is not an answer either): the state is
    # `pending`, and the callers that poll return on it at once.
    needs_approval: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return (*self.failed, *self.pending, *self.passed, *self.needs_approval)

    def merge(self, other: ChecksVerdict) -> ChecksVerdict:
        """Both verdicts as one: red beats pending beats green, names and
        counts pooled."""
        pending = (*self.pending, *other.pending)
        failed = (*self.failed, *other.failed)
        passed = (*self.passed, *other.passed)
        approval = (*self.needs_approval, *other.needs_approval)
        state: CheckState = "red" if failed else ("pending" if pending or approval else "green")
        return ChecksVerdict(state, self.total + other.total, pending, failed, passed, approval)

    def summary(self) -> str:
        if self.state == "green":
            return f"all {self.total} check(s) passed"
        if self.state == "pending":
            if self.needs_approval:
                return approval_summary(self.needs_approval)
            return f"{len(self.pending)} of {self.total} check(s) still running"
        return f"{len(self.failed)} of {self.total} check(s) failed: {', '.join(self.failed)}"


class ReviewVerdict(NamedTuple):
    """One reviewer's standing verdict on a pull request (#675)."""

    login: str
    state: str  # APPROVED | CHANGES_REQUESTED
    is_bot: bool


class FailedCheck(NamedTuple):
    """One red check run or commit status, with the text that explains it.

    ``excerpt`` is the job log (head+tail clipped) for a GitHub Actions
    check, the check's own title/summary/text for another check run, or
    the one-line ``description`` a commit status carries — what a fix round
    reads to learn *why* the build is red, not just that it is. ``url`` is
    the check's ``details_url`` / the status's ``target_url``: when the
    excerpt is empty (a status, or logs the token cannot read) it is the
    only lead the brief has (#629).
    """

    name: str
    conclusion: str
    excerpt: str
    url: str


def approval_summary(names: Sequence[str]) -> str:
    """Why a run cannot proceed on ``action_required`` checks (#612)."""
    listed = ", ".join(names)
    return (
        f"check {listed} needs a maintainer to approve the workflow run"
        if len(names) == 1
        else f"checks {listed} need a maintainer to approve their workflow runs"
    )


class BaseRequirements(NamedTuple):
    """The merge requirements of one base branch.

    ``required_contexts`` are the check names (check-run ``name`` / status
    ``context``, the shared namespace of #610) the base requires green, or
    ``None`` when a source could not be read — an unknown half can hide a
    requirement, so "some of them" is not an answer. An empty tuple is a
    real answer: both sources read, nothing declared.

    ``approvals_required`` is the number of approving reviews the base
    wants (the larger of the two sources). A positive count from either
    source is conclusive; ``0`` needs both sources read; ``None`` otherwise.
    :attr:`requires_reviews` is the same answer as a bool, for the callers
    that predate the count.

    ``source`` names where the contexts came from — ``protection``,
    ``rulesets``, ``protection+rulesets``, ``none`` (both read, nothing
    declared) or ``unknown`` — so an event or a doctor row can say so.

    The flags are what either source is *known* to require (#673); a flag
    is never conclusively off while ``source`` is ``unknown``.
    ``last_push_approval`` is fatal by construction: the loop is always
    the last pusher, so no approval can ever satisfy it.
    ``required_deployments`` names the environments a deployment must
    succeed in before the merge. ``merge_queue`` is not a blocker: the
    landing enqueues the pull request instead of merging it (#676).

    ``unread`` names the sources that could not be read (``protection``,
    ``rulesets``), so a reason or a doctor row can say which — and why:
    classic protection needs admin.

    ``forge`` names the backend that read them, so :meth:`blockers` can
    phrase each rule in that forge's own terms (:data:`BLOCKER_WORDING`).

    ``all_checks_required`` is a forge that gates on the whole pipeline
    rather than on named contexts (GitLab's "pipeline must succeed",
    field-verified on CE 19.3 for #1016): ``required_contexts`` is then
    ``()`` — nothing is *named* — and every check the head reports is
    required. A caller judging checks treats the reported set as the
    gating set (:func:`sbxloop.engine.checks.judge_checks`), and a red the
    base already had still refuses the merge, which the landing hands to a
    person rather than looping on.
    """

    required_contexts: tuple[str, ...] | None
    approvals_required: int | None
    source: str
    code_owner_review: bool = False
    last_push_approval: bool = False
    dismiss_stale_reviews: bool = False
    conversation_resolution: bool = False
    linear_history: bool = False
    signed_commits: bool = False
    merge_queue: bool = False
    required_deployments: tuple[str, ...] = ()
    unread: tuple[str, ...] = ()
    forge: str = "github"
    all_checks_required: bool = False

    @property
    def requires_reviews(self) -> bool | None:
        """Whether an approving review is required; ``None`` when unknown."""
        return None if self.approvals_required is None else self.approvals_required > 0

    def blockers(
        self,
        *,
        can_approve: bool = False,
        can_sign: bool = False,
        merge_method: str | None = None,
    ) -> list[str]:
        """Why this base cannot be landed by the loop as it is configured,
        one reason per rule, in the order a reader would fix them.

        ``can_approve`` says an approving review will come from somewhere
        (a person on GitHub the loop waits for, #675), which covers the
        code-owner review too; ``can_sign`` that the loop's
        commits arrive signed — GitHub signs commits created through its
        API only when the credential is a GitHub App; ``merge_method`` is
        the configured way to merge, so a linear-history rule blocks only
        a merge commit. Rules the loop satisfies on its own — conversation
        resolution (it resolves the threads it answers), stale-review
        dismissal and a merge queue (it enqueues, #676) — are not blockers.
        Each reason is phrased in the reading forge's own terms
        (:data:`BLOCKER_WORDING`); a forge with no entry gets the generic
        wording.
        """
        words = BLOCKER_WORDING.get(self.forge, GENERIC_WORDING)
        out: list[str] = []
        if self.last_push_approval:
            out.append(
                f"the base requires approval of the last push ({words.last_push_rule}), "
                "and the loop is always the last pusher — no approval can ever satisfy it"
            )
        if self.approvals_required and not can_approve:
            count = (
                "an approving review"
                if self.approvals_required == 1
                else f"{self.approvals_required} approving reviews"
            )
            out.append(
                f"the base requires {count}, which the loop cannot give its own pull request"
            )
        if self.code_owner_review and not can_approve:
            out.append(
                f"the base requires a review from a code owner ({words.code_owners_file}), "
                "which the loop cannot give its own pull request"
            )
        if self.signed_commits and not can_sign:
            out.append(f"the base requires signed commits; {words.signing}")
        if self.linear_history and merge_method == "merge":
            out.append(
                'the base requires a linear history and `[landing] merge_method = "merge"` '
                "would add a merge commit; use squash or rebase"
            )
        if self.required_deployments:
            envs = ", ".join(self.required_deployments)
            out.append(
                f"the base requires a successful deployment to {envs} before merging, which "
                "the loop does not run"
            )
        return out


class BlockerWording(NamedTuple):
    """The forge-specific fragments of a :meth:`BaseRequirements.blockers`
    reason: how the forge names the last-push rule, the code-owners file,
    and when (if ever) it signs the commits the loop creates through its
    API."""

    last_push_rule: str
    code_owners_file: str
    signing: str


GENERIC_WORDING = BlockerWording(
    last_push_rule="the last-push approval rule",
    code_owners_file="the code owners file",
    signing="the loop's commits, created through the forge's API, arrive unsigned",
)

# One entry per backend, keyed by its kind. The GitHub wording is the one
# the loop has always used; a reader who learnt it keeps it. The GitLab
# wording names the settings a GitLab reader knows (#1017): approvals of
# the latest pipeline's author, the CODEOWNERS file GitLab also reads, and
# the fact that commits made through the commits API arrive unsigned
# (field-verified on CE 19.3: the signature read is a 404).
BLOCKER_WORDING: dict[str, BlockerWording] = {
    "github": BlockerWording(
        last_push_rule="require_last_push_approval",
        code_owners_file="CODEOWNERS",
        signing=(
            "GitHub signs commits the loop creates through its API only when it "
            "authenticates as a GitHub App"
        ),
    ),
    "gitlab": BlockerWording(
        last_push_rule="the approval-settings rule that removes approvals on a new push",
        code_owners_file="CODEOWNERS",
        signing="GitLab does not sign commits created through its commits API",
    ),
}
