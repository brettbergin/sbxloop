"""Speak the fix round's answer back onto the pull request (#520 step 3).

A fix round already reconciles every finding of the review that seeded it —
:func:`sbxloop.engine.review.reconcile` turns the fixer's report into
``addressed`` / ``refuted`` / ``deferred`` / ``unanswered`` per anchor. Until now that
answer never left the engine: the threads GitHub opened for those findings
stayed open through the merge and the next round restated everything in a
fresh review body.

This module is the deterministic step that closes the loop, run between a
fix round's re-delivery and the next review:

* a finding with a thread gets exactly one reply — ``addressed in <sha>:
  …`` (and the thread resolved), ``refuted: …`` or a note that the round
  did not answer it (both left open);
* findings posted body-only (no anchor GitHub would take) are collected
  into one ``Reconciliation — round n`` pull request comment;
* every reply carries a machine-readable marker naming the run and round,
  so a resume between posting a reply and recording it does not double-post.

It talks to GitHub through :class:`~sbxloop.gh.ops.GithubOps` and to the
store through two small callbacks, which is what makes it testable with a
fake ops object and no database.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple, Protocol

from ..errors import GithubOpsError
from ..gh.ops import GithubOps, ReviewThread
from ..log import get_logger
from .review import (
    BLOCKING_SEVERITIES,
    CarriedVerdict,
    ReconcileStatus,
    Reconciliation,
    reconcile_anchor,
)
from .store import PostedRecord

log = get_logger(__name__)

# The pseudo-anchor the body-only round comment is recorded under, so it is
# idempotent by the same record the per-thread replies use. Not a valid
# ``path:line``, so it can never collide with a real finding.
BODY_COMMENT_KEY = "(body-only)"


def marker(run_id: str, round: int) -> str:
    """The machine-readable stamp every loop reply carries.

    An HTML comment: invisible on the rendered PR, exact to match when the
    idempotency check reads a thread's existing replies back.
    """
    return f"<!-- sbxloop:reconciled run={run_id} round={round} -->"


class Recorder(Protocol):
    """How a reply/resolve is written to the store, as it happens."""

    def __call__(self, *, anchor: str, status: str, resolved: bool) -> None: ...


class ReconcileOutcome(NamedTuple):
    """What one reconciliation pass did, for the event and the caller."""

    round: int
    addressed: int = 0
    refuted: int = 0
    unanswered: int = 0
    deferred: int = 0
    replied: int = 0
    resolved: int = 0
    body_only: int = 0
    comment_url: str | None = None
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.addressed + self.refuted + self.deferred + self.unanswered

    @property
    def did_anything(self) -> bool:
        return bool(self.replied or self.comment_url)


def reply_body(
    status: ReconcileStatus, note: str, *, head_sha: str | None, run_id: str, round: int
) -> str:
    """One thread reply: the verdict on this finding, in the loop's words."""
    note = " ".join(note.split()).strip()
    if status == "addressed":
        where = f" in {head_sha[:12]}" if head_sha else ""
        text = f"**addressed{where}**" + (f": {note}" if note else ".")
    elif status == "refuted":
        text = "**refuted**" + (f": {note}" if note else ".")
    elif status == "deferred":
        text = (
            "**deferred**" + (f": {note}" if note else "") + " — not in this pull request; "
            "noted for a follow-up. Resolving."
        )
    else:
        text = (
            f"**not answered** — fix round {round} did not report on this finding; "
            "leaving the thread open. It is carried into the next fix round as unanswered."
        )
    return f"{text}\n\n{marker(run_id, round)}"


def body_comment(
    entries: Sequence[tuple[str, Reconciliation]],
    *,
    head_sha: str | None,
    run_id: str,
    round: int,
) -> str:
    """The single PR comment that reconciles findings with no thread."""
    lines = [f"## Reconciliation — round {round}", ""]
    if head_sha:
        lines.append(f"Fix round {round} delivered `{head_sha[:12]}`.")
        lines.append("")
    lines.append(
        "These findings were posted in the review body rather than on their "
        "own thread, so their status is reported here:"
    )
    lines.append("")
    for anchor, item in entries:
        note = " ".join(item.text.split()).strip()
        suffix = f" — {note}" if note else ""
        lines.append(f"- `{anchor}` — **{item.status}**{suffix}")
    lines.append("")
    lines.append(marker(run_id, round))
    return "\n".join(lines)


def _thread_index(threads: Sequence[ReviewThread]) -> dict[int, ReviewThread]:
    index: dict[int, ReviewThread] = {}
    for thread in threads:
        for comment in thread.comments:
            if comment.comment_id is not None:
                index[comment.comment_id] = thread
    return index


def _anchor_index(threads: Sequence[ReviewThread]) -> dict[str, ReviewThread]:
    """Threads keyed by ``path:line``, for records whose capture failed.

    ``gh/ops._capture_posted`` yields ``comment_id=None`` both when GitHub
    never created an inline comment *and* when the follow-up read of the
    review's comments failed — in the latter case the thread is live on the
    PR and must be replied to, or the merge gate deadlocks on it.
    """
    index: dict[str, ReviewThread] = {}
    for thread in threads:
        if thread.root_comment_id is None:
            continue
        index.setdefault(thread.anchor, thread)
    return index


def reconcile_round(
    ops: GithubOps,
    repo: str,
    number: int,
    *,
    run_id: str,
    round: int,
    head_sha: str | None,
    posted: Sequence[PostedRecord],
    items: Mapping[str, Reconciliation],
    done: Mapping[str, str] | None = None,
    record: Recorder | None = None,
    threads: Callable[[], Sequence[ReviewThread]] | None = None,
) -> ReconcileOutcome:
    """Reply to (and where addressed, resolve) one review round's threads.

    ``posted`` is what that round put on the PR (from
    :meth:`~sbxloop.engine.store.StateStore.posted_findings`); ``items`` is
    the fix round's answer per anchor. ``done`` is what the store already
    recorded for this run/round — the first and cheapest idempotency check;
    the second reads the live threads and skips any that already carries a
    reply with this run/round marker. ``record`` is called after each reply
    lands, so a crash mid-pass resumes without repeating it.
    """
    stamp = marker(run_id, round)
    already = dict(done or {})
    fetch = threads if threads is not None else (lambda: ops.pr_review_threads(repo, number))
    loaded: tuple[dict[int, ReviewThread], dict[str, ReviewThread]] | None = None

    def live_threads() -> tuple[dict[int, ReviewThread], dict[str, ReviewThread]]:
        nonlocal loaded
        if loaded is None:
            try:
                found = list(fetch())
            except GithubOpsError:
                log.warning("review.reconcile_threads_failed", run=run_id, pr=number, exc_info=True)
                found = []
            loaded = (_thread_index(found), _anchor_index(found))
        return loaded

    counts: dict[ReconcileStatus, int] = {
        "addressed": 0,
        "refuted": 0,
        "deferred": 0,
        "unanswered": 0,
    }
    replied = resolved = skipped = 0
    body_entries: list[tuple[str, Reconciliation]] = []

    for rec in posted:
        item = items.get(rec.anchor)
        if item is None:
            continue
        counts[item.status] += 1
        comment_id = rec.comment_id
        thread: ReviewThread | None = None
        if comment_id is None:
            # Capture may have failed for a finding whose inline comment
            # GitHub did create; recover the thread by anchor before
            # treating it as body-only.
            _, by_anchor = live_threads()
            thread = by_anchor.get(rec.anchor)
            comment_id = thread.root_comment_id if thread is not None else None
        if comment_id is None:
            body_entries.append((rec.anchor, item))
            continue
        if rec.anchor in already:
            skipped += 1
            continue
        if thread is None:
            thread = live_threads()[0].get(comment_id)
        if thread is not None and thread.has_reply_marked(stamp):
            skipped += 1
            if record is not None:
                record(anchor=rec.anchor, status=item.status, resolved=thread.is_resolved)
            continue
        body = reply_body(item.status, item.text, head_sha=head_sha, run_id=run_id, round=round)
        try:
            ops.pr_comment_reply(repo, number, comment_id, body)
        except GithubOpsError:
            log.warning(
                "review.reconcile_reply_failed",
                run=run_id,
                pr=number,
                anchor=rec.anchor,
                exc_info=True,
            )
            continue
        replied += 1
        node_id = rec.thread_node_id or (thread.node_id if thread is not None else None)
        did_resolve = False
        # A finding the fixer addressed, or deliberately deferred to a
        # follow-up, is settled; refuted and unanswered threads stay open
        # for a human to have the last word.
        if item.status in ("addressed", "deferred") and node_id:
            try:
                did_resolve = ops.resolve_review_thread(node_id)
            except GithubOpsError:
                log.warning(
                    "review.reconcile_resolve_failed",
                    run=run_id,
                    pr=number,
                    anchor=rec.anchor,
                    exc_info=True,
                )
            resolved += 1 if did_resolve else 0
        if record is not None:
            record(anchor=rec.anchor, status=item.status, resolved=did_resolve)

    comment_url: str | None = None
    if body_entries and BODY_COMMENT_KEY not in already:
        text = body_comment(body_entries, head_sha=head_sha, run_id=run_id, round=round)
        try:
            comment_url = ops.pr_issue_comment(repo, number, text) or ""
        except GithubOpsError:
            log.warning("review.reconcile_comment_failed", run=run_id, pr=number, exc_info=True)
        else:
            if record is not None:
                record(anchor=BODY_COMMENT_KEY, status="posted", resolved=False)
    elif body_entries:
        skipped += 1

    return ReconcileOutcome(
        round=round,
        addressed=counts["addressed"],
        refuted=counts["refuted"],
        unanswered=counts["unanswered"],
        deferred=counts["deferred"],
        replied=replied,
        resolved=resolved,
        body_only=len(body_entries),
        comment_url=comment_url,
        skipped=skipped,
    )


def confirm_marker(run_id: str, round: int) -> str:
    """The stamp a round-*n+1* confirmation reply carries.

    Distinct from :func:`marker` so a confirmation and a reconciliation
    reply in the same thread never mistake each other for a duplicate.
    """
    return f"<!-- sbxloop:confirmed run={run_id} round={round} -->"


def confirm_body(item: CarriedVerdict, *, run_id: str, round: int) -> str:
    """One thread reply: round *n+1*'s verdict on a carried-over finding."""
    note = " ".join(item.note.split()).strip()
    if item.fixed:
        text = f"**confirmed fixed** (review round {round})" + (f": {note}" if note else ".")
    else:
        text = f"**still open** (review round {round})" + (
            f": {note}" if note else " — the fix round did not settle this."
        )
    return f"{text}\n\n{confirm_marker(run_id, round)}"


class ConfirmOutcome(NamedTuple):
    """What one pass of carried-over confirmations did."""

    round: int
    confirmed: int = 0
    still_open: int = 0
    replied: int = 0
    resolved: int = 0
    body_only: int = 0
    skipped: int = 0

    @property
    def did_anything(self) -> bool:
        return bool(self.replied or self.resolved)


def post_confirmations(
    ops: GithubOps,
    repo: str,
    number: int,
    *,
    run_id: str,
    round: int,
    items: Sequence[CarriedVerdict],
    posted: Sequence[PostedRecord],
    done: Mapping[str, str] | None = None,
    record: Recorder | None = None,
    threads: Callable[[], Sequence[ReviewThread]] | None = None,
) -> ConfirmOutcome:
    """Post round *n+1*'s verdict on each carried-over finding in its thread.

    ``posted`` is every finding this run has put on the PR (all rounds), so
    the anchor of a finding first raised in round 1 still finds its thread
    in round 3. A ``confirmed fixed`` verdict also resolves the thread; a
    ``still open`` one leaves it open — the finding is carried forward into
    the fix round instead. Findings with no thread (body-only) are counted
    and left to the review body's own summary; there is nothing to reply to.

    Idempotent the same two ways a reconciliation pass is: the store's
    record for this run/round, and the live thread's marker.
    """
    stamp = confirm_marker(run_id, round)
    already = dict(done or {})
    fetch = threads if threads is not None else (lambda: ops.pr_review_threads(repo, number))
    live: dict[int, ReviewThread] | None = None
    live_anchors: dict[str, ReviewThread] | None = None

    def live_threads() -> tuple[dict[int, ReviewThread], dict[str, ReviewThread]]:
        nonlocal live, live_anchors
        if live is None or live_anchors is None:
            try:
                found = list(fetch())
            except GithubOpsError:
                log.warning("review.confirm_threads_failed", run=run_id, pr=number, exc_info=True)
                found = []
            live, live_anchors = _thread_index(found), _anchor_index(found)
        return live, live_anchors

    by_anchor: dict[str, PostedRecord] = {}
    for rec in posted:
        # A later round's posting of the same anchor is the live thread.
        if rec.body_only and rec.anchor in by_anchor:
            continue
        by_anchor[rec.anchor] = rec

    confirmed = still_open = replied = resolved = body_only = skipped = 0
    for item in items:
        if item.fixed:
            confirmed += 1
        else:
            still_open += 1
        record_for = by_anchor.get(item.anchor)
        thread: ReviewThread | None = None
        comment_id = record_for.comment_id if record_for is not None else None
        if comment_id is None:
            # Capture failure, not absence of a thread: look the anchor up.
            thread = live_threads()[1].get(item.anchor)
            comment_id = thread.root_comment_id if thread is not None else None
        if comment_id is None:
            body_only += 1
            continue
        if item.anchor in already:
            skipped += 1
            continue
        if thread is None:
            thread = live_threads()[0].get(comment_id)
        if thread is not None and thread.has_reply_marked(stamp):
            skipped += 1
            if record is not None:
                record(anchor=item.anchor, status=item.status, resolved=thread.is_resolved)
            continue
        try:
            ops.pr_comment_reply(
                repo, number, comment_id, confirm_body(item, run_id=run_id, round=round)
            )
        except GithubOpsError:
            log.warning(
                "review.confirm_reply_failed",
                run=run_id,
                pr=number,
                anchor=item.anchor,
                exc_info=True,
            )
            continue
        replied += 1
        node_id = (record_for.thread_node_id if record_for is not None else None) or (
            thread.node_id if thread is not None else None
        )
        did_resolve = False
        if item.fixed and node_id:
            try:
                did_resolve = ops.resolve_review_thread(node_id)
            except GithubOpsError:
                log.warning(
                    "review.confirm_resolve_failed",
                    run=run_id,
                    pr=number,
                    anchor=item.anchor,
                    exc_info=True,
                )
            resolved += 1 if did_resolve else 0
        if record is not None:
            record(anchor=item.anchor, status=item.status, resolved=did_resolve)

    return ConfirmOutcome(
        round=round,
        confirmed=confirmed,
        still_open=still_open,
        replied=replied,
        resolved=resolved,
        body_only=body_only,
        skipped=skipped,
    )


def human_reply_body(
    objection: str,
    item: Reconciliation,
    *,
    head_sha: str | None,
    run_id: str,
    round: int,
) -> str:
    """The reply to one human objection.

    Same three verdicts as a loop finding, but never a resolution: a human's
    thread is theirs to close. ``unanswered`` says so plainly rather than
    claiming a fix — a fix round that did not speak to an objection is a
    fact the reviewer needs, not one to paper over.
    """
    note = " ".join(item.text.split()).strip()
    if item.status == "addressed":
        where = f" in {head_sha[:12]}" if head_sha else ""
        text = f"**addressed{where}**" + (f": {note}" if note else ".")
    elif item.status == "refuted":
        text = "**not changed**" + (f": {note}" if note else " — see the reasoning above.")
    elif item.status == "deferred":
        text = "**deferred**" + (f": {note}" if note else "") + " — not in this pull request."
    else:
        summary = " ".join(objection.split()).strip()
        text = (
            f"fix round {round} did not report specifically on this point"
            + (f" (“{summary[:200]}”)" if summary else "")
            + "; leaving it open."
        )
    return f"{text}\n\n{marker(run_id, round)}"


class HumanOutcome(NamedTuple):
    """What one pass over the human objections did."""

    round: int
    replied: int = 0
    skipped: int = 0
    body_only: int = 0
    comment_url: str | None = None
    answered: tuple[str, ...] = ()

    @property
    def did_anything(self) -> bool:
        return bool(self.replied or self.comment_url)


class HumanObjectionLike(Protocol):
    """The shape :func:`reconcile_human` needs — see
    :class:`sbxloop.engine.landing.HumanObjection`."""

    @property
    def key(self) -> str: ...
    @property
    def login(self) -> str: ...
    @property
    def body(self) -> str: ...
    @property
    def anchor(self) -> str: ...
    @property
    def comment_id(self) -> int | None: ...


class HumanRecorder(Protocol):
    """How an answered human objection is written to the store."""

    def __call__(self, *, key: str, status: str) -> None: ...


def reconcile_human(
    ops: GithubOps,
    repo: str,
    number: int,
    *,
    run_id: str,
    round: int,
    head_sha: str | None,
    objections: Sequence[HumanObjectionLike],
    report: str,
    done: Mapping[str, str] | None = None,
    record: HumanRecorder | None = None,
    threads: Callable[[], Sequence[ReviewThread]] | None = None,
) -> HumanOutcome:
    """Answer each standing human objection on its own thread.

    The fixer's ``report`` is parsed per objection anchor exactly as a loop
    finding is, so the human reads the change (or the reasoned explanation)
    where they raised the point. Two rules hold unconditionally:

    * **a human's thread is never resolved** by the loop — no
      ``resolveReviewThread`` call is made from here at all;
    * an objection answered once in this run is never answered twice —
      ``done`` (the store) and the live thread marker both gate it, and the
      keys that come back tell the caller what to persist so the next
      landing pass does not spend another fix round on the same words.
    """
    stamp = marker(run_id, round)
    already = dict(done or {})
    fetch = threads if threads is not None else (lambda: ops.pr_review_threads(repo, number))
    live: dict[int, ReviewThread] | None = None
    replied = skipped = 0
    answered: list[str] = []
    body_entries: list[tuple[str, Reconciliation]] = []

    for objection in objections:
        if objection.key in already:
            skipped += 1
            continue
        item = (
            reconcile_anchor(report, objection.anchor)
            if objection.anchor
            else Reconciliation("unanswered", "")
        )
        if objection.comment_id is None:
            body_entries.append((objection.anchor or f"@{objection.login}", item))
            answered.append(objection.key)
            if record is not None:
                record(key=objection.key, status=item.status)
            continue
        if live is None:
            try:
                live = _thread_index(fetch())
            except GithubOpsError:
                log.warning("review.reconcile_threads_failed", run=run_id, pr=number, exc_info=True)
                live = {}
        thread = live.get(objection.comment_id)
        if thread is not None and thread.has_reply_marked(stamp):
            skipped += 1
            answered.append(objection.key)
            if record is not None:
                record(key=objection.key, status=item.status)
            continue
        body = human_reply_body(objection.body, item, head_sha=head_sha, run_id=run_id, round=round)
        try:
            ops.pr_comment_reply(repo, number, objection.comment_id, body)
        except GithubOpsError:
            log.warning(
                "review.human_reply_failed",
                run=run_id,
                pr=number,
                key=objection.key,
                exc_info=True,
            )
            continue
        replied += 1
        answered.append(objection.key)
        if record is not None:
            record(key=objection.key, status=item.status)

    comment_url: str | None = None
    if body_entries:
        text = human_body_comment(body_entries, head_sha=head_sha, run_id=run_id, round=round)
        try:
            comment_url = ops.pr_issue_comment(repo, number, text) or ""
        except GithubOpsError:
            log.warning("review.human_comment_failed", run=run_id, pr=number, exc_info=True)

    return HumanOutcome(
        round=round,
        replied=replied,
        skipped=skipped,
        body_only=len(body_entries),
        comment_url=comment_url,
        answered=tuple(answered),
    )


def human_body_comment(
    entries: Sequence[tuple[str, Reconciliation]],
    *,
    head_sha: str | None,
    run_id: str,
    round: int,
) -> str:
    """The PR comment answering objections raised without a thread."""
    lines = [f"## Reconciliation — round {round} (review feedback)", ""]
    if head_sha:
        lines.append(f"Fix round {round} delivered `{head_sha[:12]}`.")
        lines.append("")
    lines.append("Answering the changes requested on this pull request:")
    lines.append("")
    for anchor, item in entries:
        note = " ".join(item.text.split()).strip()
        suffix = f" — {note}" if note else ""
        lines.append(f"- `{anchor}` — **{item.status}**{suffix}")
    lines.append("")
    lines.append(marker(run_id, round))
    return "\n".join(lines)


def noted_marker(run_id: str, round: int) -> str:
    """The stamp a "noted, not blocking" reply carries.

    Distinct from the reconciliation and confirmation markers so the three
    kinds of loop reply never mistake each other for a duplicate.
    """
    return f"<!-- sbxloop:noted run={run_id} round={round} -->"


def noted_body(finding_severity: str, *, run_id: str, round: int) -> str:
    """One thread reply on a finding of an approving review.

    Severity only changes the wording: a `major`/`blocking` finding on an
    approving verdict is an odd shape, so its reply says so plainly rather
    than calling the finding a nit.
    """
    if finding_severity in BLOCKING_SEVERITIES:
        lead = (
            f"**noted, not held against the merge** — raised as a `{finding_severity}` "
            f"on an *approving* review (round {round}), so no fix round follows it"
        )
    else:
        lead = (
            f"**noted, not blocking** — raised as a `{finding_severity}` on an approving "
            f"review (round {round}); it does not hold up the merge"
        )
    return f"{lead}. Resolving; reopen the thread if you want it acted on.\n\n" + noted_marker(
        run_id, round
    )


class NotedOutcome(NamedTuple):
    """What one pass of "noted, not blocking" replies did."""

    round: int
    noted: int = 0
    replied: int = 0
    resolved: int = 0
    body_only: int = 0
    skipped: int = 0

    @property
    def did_anything(self) -> bool:
        return bool(self.replied or self.resolved)


def note_nonblocking(
    ops: GithubOps,
    repo: str,
    number: int,
    *,
    run_id: str,
    round: int,
    findings: Mapping[str, str],
    posted: Sequence[PostedRecord],
    done: Mapping[str, str] | None = None,
    record: Recorder | None = None,
    threads: Callable[[], Sequence[ReviewThread]] | None = None,
) -> NotedOutcome:
    """Reconcile an approving round's own findings.

    An ``approve`` verdict may still carry findings of any severity, and every
    finding with a line gets its own inline thread. No fix round follows an
    approval, so nothing else in the pipeline would ever speak to those
    threads — and the merge gate of #520 step 5 refuses to merge while a
    loop thread is unanswered. Reachability, not severity, decides which
    findings need an answer: an approving verdict is free to carry a
    `major`, and that finding opens a real thread too. So the approving
    round answers them all itself:
    one reply saying the finding is noted and not blocking, and the thread
    resolved. Body-only findings have no thread and are only counted.

    ``findings`` maps anchor to its severity. Idempotent the same two ways
    the other passes are: the store's record for this run/round, and the
    live thread's marker.
    """
    stamp = noted_marker(run_id, round)
    already = dict(done or {})
    fetch = threads if threads is not None else (lambda: ops.pr_review_threads(repo, number))
    live: dict[int, ReviewThread] | None = None
    noted = replied = resolved = body_only = skipped = 0

    for rec in posted:
        severity = findings.get(rec.anchor)
        if severity is None:
            continue
        noted += 1
        if rec.body_only:
            body_only += 1
            continue
        if rec.anchor in already:
            skipped += 1
            continue
        if live is None:
            try:
                live = _thread_index(fetch())
            except GithubOpsError:
                log.warning("review.noted_threads_failed", run=run_id, pr=number, exc_info=True)
                live = {}
        assert rec.comment_id is not None
        thread = live.get(rec.comment_id)
        if thread is not None and thread.has_reply_marked(stamp):
            skipped += 1
            if record is not None:
                record(anchor=rec.anchor, status="noted", resolved=thread.is_resolved)
            continue
        try:
            ops.pr_comment_reply(
                repo, number, rec.comment_id, noted_body(severity, run_id=run_id, round=round)
            )
        except GithubOpsError:
            log.warning(
                "review.noted_reply_failed",
                run=run_id,
                pr=number,
                anchor=rec.anchor,
                exc_info=True,
            )
            continue
        replied += 1
        node_id = rec.thread_node_id or (thread.node_id if thread is not None else None)
        did_resolve = False
        if node_id:
            try:
                did_resolve = ops.resolve_review_thread(node_id)
            except GithubOpsError:
                log.warning(
                    "review.noted_resolve_failed",
                    run=run_id,
                    pr=number,
                    anchor=rec.anchor,
                    exc_info=True,
                )
            resolved += 1 if did_resolve else 0
        if record is not None:
            record(anchor=rec.anchor, status="noted", resolved=did_resolve)

    return NotedOutcome(
        round=round,
        noted=noted,
        replied=replied,
        resolved=resolved,
        body_only=body_only,
        skipped=skipped,
    )
