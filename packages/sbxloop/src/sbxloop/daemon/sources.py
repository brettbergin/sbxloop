"""Work sources: where the daemon finds work and reports back.

One implementation today. ``GitHubIssueSource`` polls the target repo for
issues carrying the trigger label and drives their lifecycle with labels
and comments — every mutation goes through the daemon's github-ops sandbox
via :class:`GithubOps`, using ``raw.api`` for label add/remove and issue
close, so no new worker ops are needed. The source never files work of its
own: an issue enters the queue only because a human labelled it (directly,
or through the Discord concierge).

Reporting is best-effort by construction: a GitHub hiccup while posting a
comment must never fail the daemon or lose an item, so every ``report_*``
swallows :class:`GithubOpsError` and logs it. Three exceptions: ``claim``'s
result decides whether a run starts, so it returns False on failure;
``poll`` raises, so the loop can back off a source that is down instead of
mistaking an outage for an empty queue; and ``report_merged`` /
``report_blocked`` return whether every step landed, so the loop keeps the
report as a debt and retries rather than recording a close that never
happened.
"""

from __future__ import annotations

import os
import re
import socket
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol
from urllib.parse import quote

from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.errors import GithubOpsError, SbxError, WorkerError
from sbxloop.gh.ops import GithubOps, raw_pages
from sbxloop.ghids import issue_item_id, try_parse_gh_id
from sbxloop.log import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sbxloop.config import RepoConfig

log = get_logger(__name__)

# The claim comment doubles as the claim lock (see GitHubIssueSource.claim);
# this hidden marker is how competing daemons recognise each other's claims.
CLAIM_MARKER = "<!-- sbxloop-claim "
# `<!-- sbxloop-claim <token> host=<host> pid=<pid> started=<iso> -->`; the
# metadata is what tells a claim from a dead process apart (#530). Claims
# written before the metadata existed still parse (empty host/pid).
_CLAIM_RE = re.compile(
    re.escape(CLAIM_MARKER)
    + r"(?P<token>[0-9a-f]{32})"
    + r"(?: host=(?P<host>\S+))?(?: pid=(?P<pid>\d+))?(?: started=(?P<started>\S+))?"
    + r" -->"
)
_STARTED_RE = re.compile(r"^Run `[^`]+` started\.")


class ClaimComment(NamedTuple):
    """One claim comment of the current trigger cycle, as GitHub lists it."""

    created: str
    comment_id: int
    token: str
    host: str
    pid: int | None


def _clean_error(text: str, limit: int = 200) -> str:
    return " ".join(text.split())[:limit]


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` is a live process on this host (signal 0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, someone else's
    except OSError:
        return True  # unknown: assume alive, never reclaim on a guess
    return True


class WorkSource(Protocol):
    name: str

    def poll(self) -> list[WorkItem]: ...
    def claim(self, item: WorkItem) -> bool: ...
    def settle_claim(self, item: WorkItem) -> bool: ...
    def report_started(self, item: WorkItem, run_id: str) -> None: ...
    def report_retry(self, item: WorkItem, error: str, attempts_left: int) -> None: ...
    def report_abandoned(self, item: WorkItem, error: str) -> None: ...
    def report_cancelled(self, item: WorkItem, report: RunReport) -> None: ...
    def report_requeued(self, item: WorkItem, by: str) -> None: ...
    def report_merged(self, item: WorkItem, pr_number: int | None, pr_url: str) -> bool: ...
    def report_blocked(
        self, item: WorkItem, reason: str, pr_number: int | None, pr_url: str
    ) -> bool: ...
    def report_gated(self, item: WorkItem, pr_number: int | None, pr_url: str) -> bool: ...


def _cancel_lines(report: RunReport) -> list[str]:
    who = report.cancelled_by or "an operator"
    lines = [f"Run `{report.run_id}` cancelled by {who} ({report.task_summary})."]
    if report.requeued:
        lines.append("Re-queued at their request; a fresh run will start on the next tick.")
    else:
        # The persisted run is left mid-flight on purpose: the human may
        # want to continue it rather than redo the work.
        lines.append(
            f"The run stays resumable: `sbxloop resume {report.run_id}` on the daemon host "
            "continues it; re-queueing runs the item again from scratch."
        )
    return lines


def _pr_ref(pr_number: int | None, pr_url: str) -> str:
    if pr_number is None:
        return "its pull request"
    return f"[PR #{pr_number}]({pr_url})" if pr_url else f"PR #{pr_number}"


# -- github issues -----------------------------------------------------------------


class GitHubLabels:
    """The six lifecycle labels. ``trigger`` puts an issue in the queue;
    ``in_progress`` is the claim marker; ``completed`` is the durable
    "sbxloop did this" mark applied when the PR merges; ``failed`` and
    ``blocked`` say the loop gave up or was refused, and both leave the
    issue open for a human; ``gated`` marks a run parked behind the opt-in
    merge gate — ready to merge, awaiting one approval."""

    def __init__(
        self,
        trigger: str,
        in_progress: str,
        failed: str,
        completed: str = "sbxloop:completed",
        blocked: str = "sbxloop:blocked",
        gated: str = "sbxloop:awaiting-merge",
    ) -> None:
        self.trigger = trigger
        self.in_progress = in_progress
        self.failed = failed
        self.completed = completed
        self.blocked = blocked
        self.gated = gated


class GitHubIssueSource:
    """Issues in the target repo carrying the trigger label are work.

    ``ops`` is a zero-arg provider (``DaemonGithub.ops``) rather than a
    fixed :class:`GithubOps`: the daemon may re-provision its sandbox at
    any time and the source must follow.
    """

    name = "github"

    def __init__(
        self,
        ops: Callable[[], GithubOps],
        repo: str,
        labels: GitHubLabels,
        *,
        host: str | None = None,
        on_failure: Callable[[BaseException], object] | None = None,
        qualify_ids: bool = False,
        extra_labels: Sequence[str] = (),
        stale_after_s: float = 300.0,
        pid: int | None = None,
        alive: Callable[[int], bool] = pid_alive,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ops = ops
        self.repo = repo
        self.labels = labels
        # A claim comment older than this with no run started after it is
        # stale; one from this host whose pid is dead is stale at once.
        self.stale_after_s = stale_after_s
        self.pid = os.getpid() if pid is None else pid
        self._alive = alive
        self._clock = clock
        # The repository's own ``labels = [...]`` (``[[github.repos]]``): added
        # to an issue alongside the in-progress mark when it is claimed. The
        # engine puts the same labels on the pull request it opens.
        self.extra_labels = tuple(extra_labels)
        # With several repositories in one daemon, issue numbers collide;
        # ids are then minted repo-qualified (``gh:<owner>/<name>:issue:<n>``).
        # A single-repo daemon keeps the historical bare form so existing
        # state, watches and operator commands resolve unchanged.
        self.qualify_ids = qualify_ids
        self.host = host or socket.gethostname()
        # Told about every failed op (``DaemonGithub.note_failure``) so a
        # dead sandbox gets replaced; the source itself never retries.
        self._on_failure = on_failure

    def _failed(self, exc: BaseException) -> None:
        if self._on_failure is not None:
            self._on_failure(exc)

    # -- helpers ----------------------------------------------------------------

    def _guard(self, what: str, fn: Callable[[GithubOps], Any]) -> Any:
        """Run a best-effort op; a GitHub failure is logged, never raised."""
        try:
            return fn(self._ops())
        except (GithubOpsError, WorkerError, SbxError) as exc:
            log.warning("github.op_failed", op=what, repo=self.repo, exc_info=True)
            self._failed(exc)
            return None

    def _issue_path(self, number: str) -> str:
        return f"/repos/{self.repo}/issues/{number}"

    def _add_label(self, ops: GithubOps, number: str, label: str) -> None:
        self._add_labels(ops, number, [label])

    def _add_labels(self, ops: GithubOps, number: str, labels: Sequence[str]) -> None:
        try:
            ops.raw("POST", f"{self._issue_path(number)}/labels", {"labels": list(labels)})
        except GithubOpsError as exc:
            raise self._label_error(exc, number, "add", labels) from exc

    def _remove_label(self, ops: GithubOps, number: str, label: str) -> None:
        try:
            ops.raw("DELETE", f"{self._issue_path(number)}/labels/{quote(label, safe='')}")
        except GithubOpsError as exc:
            # Already absent is fine (404 on the label resource). Message
            # grep is the fallback for a pre-#221 worker only.
            missing = (
                exc.http_status == 404 if exc.http_status is not None else "HTTP 404" in str(exc)
            )
            if not missing:
                raise self._label_error(exc, number, "remove", [label]) from exc

    def _label_error(
        self, exc: GithubOpsError, number: str, verb: str, labels: Sequence[str]
    ) -> GithubOpsError:
        """A label write's failure, named for what it means (#630).

        A 403 here is a *permission* gap on an otherwise working token: a
        triage-only token (or an App installed with Issues: read) can read
        issues and comment — every earlier step of the claim succeeded —
        but cannot write labels, so without this the daemon's log would
        show a bare 403 on a request the operator had no reason to expect.
        Anything else passes through as it was.
        """
        if exc.http_status != 403:
            return exc
        names = ", ".join(f"`{label}`" for label in labels)
        return GithubOpsError(
            f"cannot {verb} label(s) {names} on {self.repo}#{number}: the token lacks "
            "the permission to write issue labels (fine-grained token or GitHub App: "
            "Issues → read and write; classic PAT: `repo`) — a triage-only token can "
            f"read and comment but not label. {exc}",
            http_status=403,
        )

    def _comment(self, ops: GithubOps, number: str, body: str) -> None:
        ops.issue_comment(self.repo, int(number), body)

    # -- protocol ---------------------------------------------------------------

    def poll(self) -> list[WorkItem]:
        # Unlike the report_* paths this RAISES on failure: the loop backs
        # off a failing source (#254), which it cannot do if a GitHub outage
        # looks like an empty queue.
        label = self.labels.trigger
        query = f'repo:{self.repo} is:issue is:open label:"{label}"'
        started = time.monotonic()
        log.debug("github.poll_start", repo=self.repo, label=label)
        try:
            found = self._ops().search_issues(query, per_page=50)
        except (GithubOpsError, WorkerError, SbxError) as exc:
            log.warning(
                "github.poll_failed",
                repo=self.repo,
                label=label,
                duration_s=round(time.monotonic() - started, 2),
                error=str(exc),
            )
            self._failed(exc)
            raise
        items: list[WorkItem] = []
        seen: set[str] = set()
        for issue in found:
            number = issue.get("number")
            if not number or str(number) in seen:
                continue
            seen.add(str(number))
            items.append(
                WorkItem(
                    item_id=issue_item_id(
                        int(number), repo=self.repo if self.qualify_ids else None
                    ),
                    source_key=str(number),
                    title=str(issue.get("title") or f"issue #{number}"),
                    body=str(issue.get("body") or ""),
                    url=str(issue.get("html_url") or ""),
                    repo=self.repo,
                )
            )
        log.debug(
            "github.polled",
            repo=self.repo,
            issues=len(items),
            duration_s=round(time.monotonic() - started, 2),
        )
        return items

    def claim(self, item: WorkItem) -> bool:
        """Re-verify (search lags), take the comment lock, then swap
        trigger → in-progress.

        Two daemons watching one repo used to be able to both claim an
        issue: each re-GETs, sees the trigger, and swaps labels — the label
        writes are not conditional, so the interleaving is invisible to
        both (#254). GitHub offers no compare-and-swap on labels, but a
        comment is created exactly once and ordered, so the claim comment
        is the lock: post it first, re-read the comments, and proceed only
        if ours is the first claim comment of this trigger cycle. Cycle
        matters — a re-triggered issue carries the claim comments of its
        earlier runs, so only comments since the trigger label was last
        added count.

        The label swap is still ordered so a failure part-way can never
        lose the item: in-progress is added *before* the trigger is removed
        (both present is a safe intermediate — polling still finds it), and
        if removing the trigger fails the in-progress label and our claim
        comment are rolled back so a later claimer is not locked out.
        """
        number = item.source_key
        trigger = self.labels.trigger
        added_in_progress = False
        stale: list[str] = []
        comment_id: int | None = None
        started = time.monotonic()
        log.debug("github.claim_start", item=item.item_id, repo=self.repo, trigger=trigger)
        try:
            ops = self._ops()
            issue = ops.raw("GET", self._issue_path(number))
            if not isinstance(issue, dict) or issue.get("state") != "open":
                log.info(
                    "github.claim_declined",
                    item=item.item_id,
                    reason="issue no longer open",
                    state=issue.get("state") if isinstance(issue, dict) else None,
                )
                return False
            names = {
                label.get("name") for label in issue.get("labels") or [] if isinstance(label, dict)
            }
            if trigger not in names:
                log.info(
                    "github.claim_declined",
                    item=item.item_id,
                    reason="trigger label gone (search lag or already claimed)",
                    trigger=trigger,
                    labels=sorted(str(n) for n in names if n),
                )
                return False
            epoch = self._trigger_epoch(ops, number, trigger)
            # The daemon persisted the token before calling (#530), so a
            # crash between the comment and the persist is recoverable.
            token = item.claim_token or uuid.uuid4().hex
            self._comment(ops, number, self._claim_body(token, item, names))
            claims = self._claims(ops, number, epoch)
            mine = next((c for c in claims if c.token == token), None)
            comment_id = mine.comment_id if mine else None
            # Earlier claims from a process that is gone are not live claims:
            # release them, then judge the race among the live ones.
            live = []
            for claim in claims:
                if claim.token != token and self._stale(claim, claims, ops, number):
                    log.warning(
                        "github.claim_reclaimed",
                        item=item.item_id,
                        stale_token=claim.token,
                        stale_host=claim.host or None,
                        stale_pid=claim.pid,
                        hint="an earlier claim comment from a process that is gone; released",
                    )
                    self._delete_comment_quietly(number, claim.comment_id)
                    continue
                live.append(claim)
            first_token = live[0].token if live else None
            if first_token != token:
                log.info(
                    "github.claim_lost_race",
                    item=item.item_id,
                    winner=first_token,
                    duration_s=round(time.monotonic() - started, 2),
                )
                self._delete_comment_quietly(number, comment_id)
                return False
            # A restart-by-label (#600) arrives wearing the previous
            # attempt's lifecycle label. Those describe a run that is over:
            # clear the ones the GET actually showed, so the claim leaves
            # the issue in the same shape a first-time claim does. The
            # trigger swap keeps its ordering (in-progress on before the
            # trigger comes off) — a crash between the two must never leave
            # an issue that polling can no longer find.
            stale = [
                label
                for label in (self.labels.failed, self.labels.blocked, self.labels.completed)
                if label in names
            ]
            for label in stale:
                self._remove_label(ops, number, label)
            self._add_labels(ops, number, [self.labels.in_progress, *self.extra_labels])
            added_in_progress = True
            self._remove_label(ops, number, trigger)
        except (GithubOpsError, WorkerError, SbxError) as exc:
            log.warning(
                "github.claim_failed",
                item=item.item_id,
                repo=self.repo,
                error=str(exc),
                rolling_back_label=added_in_progress,
                duration_s=round(time.monotonic() - started, 2),
                exc_info=True,
            )
            self._failed(exc)
            if added_in_progress:
                # Best-effort: leave the issue exactly as we found it.
                for added in (self.labels.in_progress, *self.extra_labels):
                    self._guard(
                        "claim rollback",
                        partial(self._remove_label, number=number, label=added),
                    )
                if stale:
                    self._guard(
                        "claim rollback",
                        partial(self._add_labels, number=number, labels=stale),
                    )
            self._delete_comment_quietly(number, comment_id)
            return False
        log.info(
            "github.claimed",
            item=item.item_id,
            repo=self.repo,
            duration_s=round(time.monotonic() - started, 2),
        )
        return True

    def _trigger_epoch(self, ops: GithubOps, number: str, trigger: str | None = None) -> str:
        """ISO timestamp of the trigger label's most recent addition — the
        start of the current claim cycle. Empty (every claim comment
        counts) if the issue's events do not show one."""
        trigger = trigger or self.labels.trigger
        latest = ""
        for event in raw_pages(ops, f"{self._issue_path(number)}/events"):
            if not isinstance(event, dict) or event.get("event") != "labeled":
                continue
            label = event.get("label")
            if isinstance(label, dict) and label.get("name") == trigger:
                latest = max(latest, str(event.get("created_at") or ""))
        return latest

    def _claim_body(
        self, token: str, item: WorkItem | None = None, names: set[Any] | None = None
    ) -> str:
        """The claim comment: the hidden lock marker plus what a human needs
        to read off the issue trail — that this is a claim, and (#600)
        whether it is a restart driven by the trigger label being re-added
        and what pushed work that restart continues."""
        started = datetime.fromtimestamp(self._clock(), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            f"{CLAIM_MARKER}{token} host={self.host} pid={self.pid} started={started} -->",
            f"sbxloop daemon claimed this issue (host `{self.host}`).",
        ]
        prior_labels = sorted(
            str(n)
            for n in (names or set())
            if n in {self.labels.failed, self.labels.blocked, self.labels.completed}
        )
        marks = ", ".join(f"`{n}`" for n in prior_labels)
        # Only the *store* knows whether this issue was attempted before.
        # A lifecycle label alone does not make a claim a restart: a human
        # can hand-label an issue `sbxloop:failed` and calling its first-ever
        # claim a restart would be a false trail. The labels only name what
        # this claim cleared.
        if item is not None and item.restarted:
            lines.append(
                f"Restarted by re-adding `{self.labels.trigger}`"
                + (f" (clearing {marks} from the previous attempt)" if marks else "")
                + "; the issue did not need to be edited."
            )
            lines.append(self._reuse_line(item))
        elif marks:
            lines.append(f"Cleared {marks} from an earlier lifecycle state.")
        return "\n".join(lines)

    def _reuse_line(self, item: WorkItem | None) -> str:
        branch = item.prior_branch if item else None
        pr = item.prior_pr_number if item else None
        if branch and pr is not None:
            return (
                f"Continuing the work the previous attempt pushed: branch `{branch}` and PR #{pr}."
            )
        if branch:
            return f"Continuing the work the previous attempt pushed: branch `{branch}`."
        if pr is not None:
            return f"Continuing the previous attempt's PR #{pr}."
        return "No branch or PR from a previous attempt was recorded; starting fresh."

    def _claims(self, ops: GithubOps, number: str, epoch: str) -> list[ClaimComment]:
        """The claim comments of this trigger cycle, oldest first. Ordered by
        GitHub's own timestamps so host clock skew cannot decide the race;
        ids break same-second ties. ``self._last_comments`` keeps the raw
        rows for :meth:`_stale`, which needs to see what came after."""
        claims: list[ClaimComment] = []
        rows: list[dict[str, Any]] = []
        for comment in raw_pages(ops, f"{self._issue_path(number)}/comments"):
            if not isinstance(comment, dict):
                continue
            rows.append(comment)
            match = _CLAIM_RE.search(str(comment.get("body") or ""))
            created = str(comment.get("created_at") or "")
            if match is None or created < epoch:
                continue
            pid = match.group("pid")
            claims.append(
                ClaimComment(
                    created,
                    int(comment.get("id") or 0),
                    match.group("token"),
                    match.group("host") or "",
                    int(pid) if pid else None,
                )
            )
        self._last_comments = rows
        claims.sort()
        return claims

    def _stale(
        self, claim: ClaimComment, claims: Sequence[ClaimComment], ops: GithubOps, number: str
    ) -> bool:
        """A claim from a process that is gone (#530): from this host with
        a dead pid, or older than ``stale_after_s`` with no run started
        after it — a live claimer starts its run within a poll interval."""
        if (
            claim.host == self.host
            and claim.pid is not None
            and claim.pid != self.pid
            and not self._alive(claim.pid)
        ):
            return True
        if self.stale_after_s <= 0:
            return False
        try:
            created = datetime.strptime(claim.created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:
            return False
        if self._clock() - created.timestamp() < self.stale_after_s:
            return False
        rows = getattr(self, "_last_comments", [])
        for row in rows:
            body = str(row.get("body") or "")
            if str(row.get("created_at") or "") >= claim.created and _STARTED_RE.match(body):
                return False  # its run did start; a stuck run is recovery's job, not ours
        return True

    def settle_claim(self, item: WorkItem) -> bool:
        """Finish or forget a half-claim after a restart (#530).

        The token was persisted before the comment was posted; whether the
        comment landed is the question. Present → the claim is ours: make
        sure the labels are swapped and report it claimed. Absent → nothing
        reached the source; the caller clears the token and the next tick
        claims from scratch. A GitHub failure reads as absent — a re-claim
        is cheap, an orphaned issue is not.
        """
        token = item.claim_token
        if not token:
            return False
        number = item.source_key
        try:
            ops = self._ops()
            epoch = self._trigger_epoch(ops, number)
            claims = self._claims(ops, number, epoch)
            if not any(c.token == token for c in claims):
                # Also look outside the cycle window: a claim from before a
                # re-trigger is still ours, just no longer relevant.
                return False
            issue = ops.raw("GET", self._issue_path(number))
            names = {
                label.get("name")
                for label in (issue.get("labels") if isinstance(issue, dict) else []) or []
                if isinstance(label, dict)
            }
            if self.labels.in_progress not in names:
                self._add_labels(ops, number, [self.labels.in_progress, *self.extra_labels])
            if self.labels.trigger in names:
                self._remove_label(ops, number, self.labels.trigger)
        except (GithubOpsError, WorkerError, SbxError) as exc:
            log.warning("github.claim_settle_failed", item=item.item_id, exc_info=True)
            self._failed(exc)
            return False
        log.info("github.claim_settled", item=item.item_id, repo=self.repo, token=token)
        return True

    def _delete_comment_quietly(self, number: str, comment_id: int | None) -> None:
        """Release the comment lock after a lost race or failed claim; a
        stray claim comment would lock every later claimer out of this
        cycle. Best-effort: nothing to do if we never learned its id."""
        if comment_id is None:
            return
        self._guard(
            "claim comment removal",
            lambda ops: ops.raw("DELETE", f"/repos/{self.repo}/issues/comments/{comment_id}"),
        )

    def report_started(self, item: WorkItem, run_id: str) -> None:
        self._guard(
            "start comment",
            lambda ops: self._comment(ops, item.source_key, f"Run `{run_id}` started."),
        )

    def report_merged(self, item: WorkItem, pr_number: int | None, pr_url: str) -> bool:
        """The run merged its PR: the work landed. Close the source issue
        and leave ``labels.completed`` as the durable mark.

        Returns True only when every step succeeded, so the caller keeps the
        report as a debt and retries an interrupted settle instead of
        recording it as done. Labels come before the close: a failure
        mid-way leaves an open, correctly labelled issue rather than a
        closed one with no mark.
        """

        def go(ops: GithubOps) -> bool:
            n = item.source_key
            self._comment(
                ops, n, f"{_pr_ref(pr_number, pr_url)} was merged — work completed by sbxloop."
            )
            self._remove_label(ops, n, self.labels.in_progress)
            self._remove_label(ops, n, self.labels.gated)
            self._add_label(ops, n, self.labels.completed)
            # Blind PATCH, no state pre-read: the PR body's `Closes #N` may
            # have closed the issue already, and re-closing a closed issue
            # is a no-op success.
            ops.raw("PATCH", self._issue_path(n), {"state": "closed", "state_reason": "completed"})
            return True

        return bool(self._guard("merge report", go))

    def report_blocked(
        self, item: WorkItem, reason: str, pr_number: int | None, pr_url: str
    ) -> bool:
        """The run cleared its own bar but GitHub would not let it finish.
        The issue stays open, marked blocked, for a human: merge or fix the
        PR by hand and close the issue, or re-add the trigger label once
        whatever refused has been dealt with (#600) — that restarts it on
        this same branch and PR."""

        def go(ops: GithubOps) -> bool:
            n = item.source_key
            self._comment(
                ops,
                n,
                f"sbxloop could not finish: {reason}\n\n{_pr_ref(pr_number, pr_url)} passed "
                "the loop's own review and checks but GitHub would not let the loop land it. "
                "A human needs to look: merge or fix it by hand and close this issue, or "
                f"re-add `{self.labels.trigger}` once the cause is dealt with — the issue "
                f"does not need to be edited and `{self.labels.blocked}` does not need "
                "removing by hand (the claim clears it), and the restart continues on this "
                "branch and pull request.",
            )
            self._remove_label(ops, n, self.labels.in_progress)
            # A trigger label still on the issue would make the human's
            # re-add a no-op — GitHub fires no event for a label already
            # there, which is how the label went inert (#596).
            self._remove_label(ops, n, self.labels.trigger)
            self._add_label(ops, n, self.labels.blocked)
            return True

        return bool(self._guard("blocked report", go))

    def report_gated(self, item: WorkItem, pr_number: int | None, pr_url: str) -> bool:
        """The run parked behind the opt-in merge gate ([landing]
        merge_gate): ready to merge, awaiting one human approval. The issue
        stays open and in progress — nothing failed, and the work is not
        done until the merge lands."""

        def go(ops: GithubOps) -> bool:
            n = item.source_key
            self._comment(
                ops,
                n,
                f"{_pr_ref(pr_number, pr_url)} is ready and green — sbxloop is parked "
                "awaiting merge approval (`[landing] merge_gate`). Approve from the "
                f"run's chat thread, with `!sbx merge {item.item_id}` in chat, or with "
                f"`sbxloop daemon ctl merge {item.item_id}` on the daemon host; "
                f"`!sbx abandon {item.item_id}` declines and leaves the PR open. "
                "There is no deadline.",
            )
            self._add_label(ops, n, self.labels.gated)
            return True

        return bool(self._guard("gated report", go))

    def report_retry(self, item: WorkItem, error: str, attempts_left: int) -> None:
        self._guard(
            "retry comment",
            lambda ops: self._comment(
                ops,
                item.source_key,
                f"Run failed: {error}\n\n{attempts_left} attempt(s) remaining; will retry.",
            ),
        )

    def report_abandoned(self, item: WorkItem, error: str) -> None:
        def go(ops: GithubOps) -> None:
            n = item.source_key
            self._comment(
                ops,
                n,
                f"Abandoned after retries: {error}\n\nRe-add "
                f"`{self.labels.trigger}` to run it again — the issue does not need to be "
                f"edited and `{self.labels.failed}` does not need to be removed by hand "
                "(the claim clears it), and the restart continues from any branch or "
                "PR a previous attempt pushed.",
            )
            self._remove_label(ops, n, self.labels.in_progress)
            # Always clear the trigger, claimed or not: left on the issue it
            # keeps the item polling as work, and it makes the human's
            # re-add a no-op — GitHub fires no event for a label already
            # there, which is exactly how the label went inert (#596).
            self._remove_label(ops, n, self.labels.trigger)
            self._add_label(ops, n, self.labels.failed)

        self._guard("abandon report", go)

    def report_cancelled(self, item: WorkItem, report: RunReport) -> None:
        def go(ops: GithubOps) -> None:
            n = item.source_key
            lines = _cancel_lines(report)
            if not report.requeued:
                # Neither failed nor triggered: the human decides what
                # happens next, so no label speaks for them. Re-adding the
                # trigger label is the self-service way back — the store
                # re-queues a finished item whether or not the issue text
                # changed (#600) and the restart continues on whatever
                # branch/PR this attempt already pushed.
                lines.append(
                    f"To continue it: re-add `{self.labels.trigger}` — the next poll picks "
                    "the issue back up and resumes from the branch and PR this run already "
                    f"pushed. `!sbx retry {item.item_id}` in Discord restarts it from scratch "
                    "instead."
                )
            self._comment(ops, n, "\n".join(lines))
            if not report.requeued:
                self._remove_label(ops, n, self.labels.in_progress)
                # A trigger label still on the issue (cancelled before the
                # claim swap landed, or re-added mid-run) would make the
                # human's re-add a no-op — GitHub does not re-fire an event
                # for a label that is already there (#596). Clear it so the
                # documented restart gesture actually works.
                self._remove_label(ops, n, self.labels.trigger)

        self._guard("cancel report", go)

    def report_requeued(self, item: WorkItem, by: str) -> None:
        def go(ops: GithubOps) -> None:
            n = item.source_key
            # A failed item carries the failed label, a blocked one the
            # blocked label, a done one completed — all describe the
            # previous run, not the one about to start (absent → 404,
            # tolerated). Strip them *before* adding in-progress: `_guard`
            # swallows a failure mid-way, and adding first would leave the
            # issue wearing two lifecycle labels.
            self._remove_label(ops, n, self.labels.failed)
            self._remove_label(ops, n, self.labels.blocked)
            self._remove_label(ops, n, self.labels.completed)
            # in-progress is the claim marker; a re-queued item is claimed
            # again without a fresh label swap.
            self._add_label(ops, n, self.labels.in_progress)
            self._comment(ops, n, f"Re-queued by {by}; a fresh run will start shortly.")

        self._guard("requeue report", go)


# -- many repositories -------------------------------------------------------------


# Per-repository failure accounting (#516). Transient failures back off per
# repo; a permanent one — or enough consecutive ones — suspends the repo.
REPO_HEALTH_KEY = "repo_health:"
PERMANENT_STATUSES: frozenset[int] = frozenset({404, 410})


def permanent_failure(exc: BaseException) -> bool:
    """Whether GitHub said this repository is gone for this token: 404/410,
    or a 403 that is a permission refusal rather than a rate limit."""
    status = getattr(exc, "http_status", None)
    if status in PERMANENT_STATUSES:
        return True
    if status == 403:
        text = str(exc).lower()
        return "rate limit" not in text and "abuse" not in text and "secondary" not in text
    return False


class RepoHealth(NamedTuple):
    """One repository's polling health, as ``status`` and ``doctor`` show it."""

    repo: str
    failures: int = 0
    next_poll: float | None = None  # backing off until then (None: polled every tick)
    suspended: bool = False
    reason: str = ""
    since: float | None = None

    @property
    def state(self) -> str:
        if self.suspended:
            return "suspended"
        return "backoff" if self.next_poll is not None else "ok"

    def to_json(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "failures": self.failures,
            "next_poll": self.next_poll,
            "suspended": self.suspended,
            "reason": self.reason,
            "since": self.since,
        }


class MultiRepoIssueSource:
    """One :class:`GitHubIssueSource` per configured repository, fanned out.

    Discovery polls every repository in order and concatenates the results,
    so ordering across repos is deterministic (configuration order, then the
    per-repo poll order). Every other operation is routed back to the source
    that owns the item's repository, so a claim, comment or label always
    lands on the repository the work came from.

    A failure polling one repository is logged and skipped rather than
    dropping the other repositories' items; only a *total* failure (every
    configured repository failed) re-raises, preserving the single-repo
    contract that the loop backs a failing source off instead of mistaking
    an outage for an empty queue.

    Per-repository health (#516): a repository that keeps failing is backed
    off on its own — skipped for ``poll_interval_s * 2**(failures-1)``,
    capped at an hour — and after ``suspend_after`` consecutive failures,
    or at once when GitHub says it is gone for this token (404/410, a
    permission 403), it is **suspended**: excluded from polling until an
    operator resumes it (``ctl resume-repo``) or the daemon restarts with a
    changed configuration. Healthy neighbours are never punished. The
    state is narrated once per transition, not once per tick, and handed
    to ``persist`` so ``doctor`` in another process can show it.
    """

    name = "github"

    def __init__(
        self,
        sources: list[GitHubIssueSource],
        *,
        poll_interval_s: float = 60.0,
        suspend_after: int = 10,
        clock: Callable[[], float] = time.time,
        persist: Callable[[str, dict[str, Any] | None], None] | None = None,
        notify: Callable[[str, str, str], None] | None = None,
    ) -> None:
        if not sources:
            raise ValueError("MultiRepoIssueSource needs at least one repository source")
        self._sources = list(sources)
        self._by_repo = {s.repo.casefold(): s for s in self._sources}
        self.poll_interval_s = poll_interval_s
        self.suspend_after = suspend_after
        self._clock = clock
        self._persist = persist
        self._notify = notify
        self._health: dict[str, RepoHealth] = {s.repo: RepoHealth(s.repo) for s in self._sources}

    @property
    def sources(self) -> list[GitHubIssueSource]:
        return list(self._sources)

    @property
    def repos(self) -> list[str]:
        return [s.repo for s in self._sources]

    @property
    def repo(self) -> str:
        """The first repository — what a single-repo caller means by "the repo"."""
        return self._sources[0].repo

    @property
    def labels(self) -> GitHubLabels:
        return self._sources[0].labels

    def for_item(self, item: WorkItem) -> GitHubIssueSource:
        """The source owning ``item``'s repository.

        Falls back to the sole configured source when the item carries no
        repository (persisted before multi-repo support, or a legacy
        ``gh:<n>`` id), which is exactly the single-repo behaviour.
        """
        repo = item.repo
        if repo is None:
            parsed = try_parse_gh_id(item.item_id)
            repo = parsed.repo if parsed is not None else None
        if repo is not None:
            found = self._by_repo.get(repo.casefold())
            if found is not None:
                return found
            log.warning(
                "github.unknown_repo",
                item=item.item_id,
                repo=repo,
                known=self.repos,
            )
        return self._sources[0]

    # -- per-repository health ------------------------------------------------

    @property
    def notify(self) -> Callable[[str, str, str], None] | None:
        return self._notify

    @notify.setter
    def notify(self, fn: Callable[[str, str, str], None] | None) -> None:
        # Set after the loop exists: the source is built before it.
        self._notify = fn

    @property
    def repo_health(self) -> list[RepoHealth]:
        return [self._health[s.repo] for s in self._sources]

    def _set_health(self, health: RepoHealth) -> None:
        self._health[health.repo] = health
        if self._persist is not None:
            self._persist(health.repo, None if health.state == "ok" else health.to_json())

    def _say(self, kind: str, repo: str, text: str) -> None:
        if self._notify is not None:
            self._notify(kind, repo, text)

    def _record_failure(self, source: GitHubIssueSource, exc: BaseException) -> None:
        now = self._clock()
        before = self._health[source.repo]
        failures = before.failures + 1
        reason = _clean_error(str(exc))
        permanent = permanent_failure(exc)
        if permanent or failures >= self.suspend_after:
            why = (
                f"GitHub says the repository is gone for this token ({reason})"
                if permanent
                else f"{failures} consecutive poll failures, last: {reason}"
            )
            self._set_health(
                RepoHealth(source.repo, failures, None, True, why, before.since or now)
            )
            log.error(
                "github.repo_suspended",
                repo=source.repo,
                failures=failures,
                permanent=permanent,
                error=reason,
                hint="excluded from polling until `ctl resume-repo <repo>` or a restart with "
                "a changed configuration",
            )
            self._say(
                "source.repo_suspended",
                source.repo,
                f"🚫 repository {source.repo} suspended from polling: {why} — "
                f"`resume-repo {source.repo}` once it is fixed",
            )
            return
        delay = min(self.poll_interval_s * 2 ** (failures - 1), 3600.0)
        health = RepoHealth(source.repo, failures, now + delay, False, reason, before.since or now)
        self._set_health(health)
        if failures == 1:
            log.warning(
                "github.repo_poll_failed",
                repo=source.repo,
                error=reason,
                next_poll_in_s=round(delay),
                hint="backing this repository off on its own; the others poll as usual",
            )
        else:
            log.warning(
                "github.repo_backoff",
                repo=source.repo,
                failures=failures,
                next_poll_in_s=round(delay),
                error=reason,
            )

    def _record_success(self, source: GitHubIssueSource) -> None:
        before = self._health[source.repo]
        if before.state == "ok":
            return
        self._set_health(RepoHealth(source.repo))
        log.info("github.repo_poll_recovered", repo=source.repo, after_failures=before.failures)
        self._say(
            "source.repo_recovered",
            source.repo,
            f"repository {source.repo} polls again after {before.failures} failure(s)",
        )

    def resume_repo(self, repo: str) -> RepoHealth:
        """Operator: poll a suspended (or backing-off) repository again now."""
        source = self._by_repo.get(repo.casefold())
        if source is None:
            raise KeyError(f"unknown repository {repo!r}; configured: {', '.join(self.repos)}")
        before = self._health[source.repo]
        if before.state == "ok":
            raise ValueError(f"{source.repo} is not suspended or backing off")
        self._set_health(RepoHealth(source.repo))
        log.info(
            "github.repo_resumed", repo=source.repo, was=before.state, failures=before.failures
        )
        return self._health[source.repo]

    def poll(self) -> list[WorkItem]:
        items: list[WorkItem] = []
        failures: list[BaseException] = []
        now = self._clock()
        polled = 0
        for source in self._sources:
            health = self._health[source.repo]
            if health.suspended or (health.next_poll is not None and now < health.next_poll):
                continue  # its own clock, not the loop's (#516)
            polled += 1
            try:
                items.extend(source.poll())
            except (GithubOpsError, WorkerError, SbxError) as exc:
                # Accounted per repo and skipped: one unreachable repository
                # must not blank the queue for the healthy ones.
                self._record_failure(source, exc)
                failures.append(exc)
            else:
                self._record_success(source)
        if failures and polled and len(failures) == polled:
            raise failures[0]
        return items

    def claim(self, item: WorkItem) -> bool:
        return self.for_item(item).claim(item)

    def settle_claim(self, item: WorkItem) -> bool:
        return self.for_item(item).settle_claim(item)

    def report_started(self, item: WorkItem, run_id: str) -> None:
        self.for_item(item).report_started(item, run_id)

    def report_retry(self, item: WorkItem, error: str, attempts_left: int) -> None:
        self.for_item(item).report_retry(item, error, attempts_left)

    def report_abandoned(self, item: WorkItem, error: str) -> None:
        self.for_item(item).report_abandoned(item, error)

    def report_cancelled(self, item: WorkItem, report: RunReport) -> None:
        self.for_item(item).report_cancelled(item, report)

    def report_requeued(self, item: WorkItem, by: str) -> None:
        self.for_item(item).report_requeued(item, by)

    def report_merged(self, item: WorkItem, pr_number: int | None, pr_url: str) -> bool:
        return self.for_item(item).report_merged(item, pr_number, pr_url)

    def report_blocked(
        self, item: WorkItem, reason: str, pr_number: int | None, pr_url: str
    ) -> bool:
        return self.for_item(item).report_blocked(item, reason, pr_number, pr_url)

    def report_gated(self, item: WorkItem, pr_number: int | None, pr_url: str) -> bool:
        return self.for_item(item).report_gated(item, pr_number, pr_url)


def build_github_source(
    ops: Callable[[], GithubOps],
    repos: Sequence[RepoConfig],
    labels: GitHubLabels,
    *,
    host: str | None = None,
    on_failure: Callable[[BaseException], object] | None = None,
    stale_after_s: float = 300.0,
    poll_interval_s: float = 60.0,
    suspend_after: int = 10,
    persist: Callable[[str, dict[str, Any] | None], None] | None = None,
    notify: Callable[[str, str, str], None] | None = None,
) -> WorkSource:
    """A work source over every *enabled* repository in ``repos``.

    A single enabled repository yields a plain :class:`GitHubIssueSource`
    minting unqualified ids — byte-for-byte the pre-multi-repo behaviour.
    Two or more yield a :class:`MultiRepoIssueSource` whose items carry
    repo-qualified ids so issue numbers from different repositories cannot
    collide.
    """
    enabled = [entry for entry in repos if entry.enabled]
    if not enabled:
        raise ValueError("no enabled repository configured for the daemon to poll")
    qualify = len(enabled) > 1
    built = [
        GitHubIssueSource(
            ops,
            entry.repo,
            _repo_labels(labels, entry),
            host=host,
            on_failure=on_failure,
            qualify_ids=qualify,
            extra_labels=entry.labels,
            stale_after_s=stale_after_s,
        )
        for entry in enabled
    ]
    if len(built) == 1:
        return built[0]
    return MultiRepoIssueSource(
        built,
        poll_interval_s=poll_interval_s,
        suspend_after=suspend_after,
        persist=persist,
        notify=notify,
    )


def _repo_labels(labels: GitHubLabels, entry: RepoConfig) -> GitHubLabels:
    """The daemon labels with the repository's overrides applied — a
    straight merge of the entry's six ``<kind>_label`` fields over the
    daemon-wide set (#630)."""
    return GitHubLabels(
        entry.trigger_label or labels.trigger,
        entry.in_progress_label or labels.in_progress,
        entry.failed_label or labels.failed,
        entry.completed_label or labels.completed,
        entry.blocked_label or labels.blocked,
        entry.gated_label or labels.gated,
    )
