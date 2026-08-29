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

import re
import socket
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any, Protocol
from urllib.parse import quote

from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.errors import GithubOpsError, SbxError, WorkerError
from sbxloop.gh.ops import GithubOps
from sbxloop.log import get_logger

log = get_logger(__name__)

# The claim comment doubles as the claim lock (see GitHubIssueSource.claim);
# this hidden marker is how competing daemons recognise each other's claims.
CLAIM_MARKER = "<!-- sbxloop-claim "
_CLAIM_RE = re.compile(re.escape(CLAIM_MARKER) + r"([0-9a-f]{32}) -->")
# GitHub list endpoints page at 100; an issue with more history than this
# many pages is not one the daemon should be arbitrating by comment anyway.
_MAX_PAGES = 10


class WorkSource(Protocol):
    name: str

    def poll(self) -> list[WorkItem]: ...
    def claim(self, item: WorkItem) -> bool: ...
    def report_started(self, item: WorkItem, run_id: str) -> None: ...
    def report_retry(self, item: WorkItem, error: str, attempts_left: int) -> None: ...
    def report_abandoned(self, item: WorkItem, error: str) -> None: ...
    def report_cancelled(self, item: WorkItem, report: RunReport) -> None: ...
    def report_requeued(self, item: WorkItem, by: str) -> None: ...
    def report_merged(self, item: WorkItem, pr_number: int | None, pr_url: str) -> bool: ...
    def report_blocked(
        self, item: WorkItem, reason: str, pr_number: int | None, pr_url: str
    ) -> bool: ...


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
    """The five lifecycle labels. ``trigger`` puts an issue in the queue;
    ``in_progress`` is the claim marker; ``completed`` is the durable
    "sbxloop did this" mark applied when the PR merges; ``failed`` and
    ``blocked`` say the loop gave up or was refused, and both leave the
    issue open for a human."""

    def __init__(
        self,
        trigger: str,
        in_progress: str,
        failed: str,
        completed: str = "sbxloop:completed",
        blocked: str = "sbxloop:blocked",
    ) -> None:
        self.trigger = trigger
        self.in_progress = in_progress
        self.failed = failed
        self.completed = completed
        self.blocked = blocked


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
    ) -> None:
        self._ops = ops
        self.repo = repo
        self.labels = labels
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
        ops.raw("POST", f"{self._issue_path(number)}/labels", {"labels": [label]})

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
                raise

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
                    item_id=f"gh:{number}",
                    source_key=str(number),
                    title=str(issue.get("title") or f"issue #{number}"),
                    body=str(issue.get("body") or ""),
                    url=str(issue.get("html_url") or ""),
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
            token = uuid.uuid4().hex
            self._comment(
                ops,
                number,
                f"{CLAIM_MARKER}{token} -->\n"
                f"sbxloop daemon claimed this issue (host `{self.host}`).",
            )
            comment_id, first_token = self._first_claim(ops, number, epoch, token)
            if first_token != token:
                log.info(
                    "github.claim_lost_race",
                    item=item.item_id,
                    winner=first_token,
                    duration_s=round(time.monotonic() - started, 2),
                )
                self._delete_comment_quietly(number, comment_id)
                return False
            self._add_label(ops, number, self.labels.in_progress)
            added_in_progress = True
            self._remove_label(ops, number, trigger)
        except (GithubOpsError, WorkerError, SbxError) as exc:
            log.warning(
                "github.claim_failed",
                item=item.item_id,
                repo=self.repo,
                rolling_back_label=added_in_progress,
                duration_s=round(time.monotonic() - started, 2),
                exc_info=True,
            )
            self._failed(exc)
            if added_in_progress:
                # Best-effort: leave the issue exactly as we found it.
                self._guard(
                    "claim rollback",
                    lambda ops: self._remove_label(ops, number, self.labels.in_progress),
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
        for events in self._pages(ops, f"{self._issue_path(number)}/events"):
            for event in events:
                if not isinstance(event, dict) or event.get("event") != "labeled":
                    continue
                label = event.get("label")
                if isinstance(label, dict) and label.get("name") == trigger:
                    latest = max(latest, str(event.get("created_at") or ""))
        return latest

    def _first_claim(
        self, ops: GithubOps, number: str, epoch: str, token: str
    ) -> tuple[int | None, str | None]:
        """(id of OUR claim comment if found, token of the FIRST claim
        comment of this cycle). Ordered by GitHub's own timestamps so host
        clock skew cannot decide the race; ids break same-second ties."""
        claims: list[tuple[str, int, str]] = []
        for comments in self._pages(ops, f"{self._issue_path(number)}/comments"):
            for comment in comments:
                if not isinstance(comment, dict):
                    continue
                match = _CLAIM_RE.search(str(comment.get("body") or ""))
                created = str(comment.get("created_at") or "")
                if match is None or created < epoch:
                    continue
                claims.append((created, int(comment.get("id") or 0), match.group(1)))
        claims.sort()
        mine = next((cid for _, cid, tok in claims if tok == token), None)
        return mine, claims[0][2] if claims else None

    def _pages(self, ops: GithubOps, path: str) -> Iterator[list[Any]]:
        for page in range(1, _MAX_PAGES + 1):
            data = ops.raw("GET", f"{path}?per_page=100&page={page}")
            if not isinstance(data, list) or not data:
                return
            yield data
            if len(data) < 100:
                return

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
        PR by hand and close the issue, or ``!sbx retry`` it once whatever
        refused has been dealt with."""

        def go(ops: GithubOps) -> bool:
            n = item.source_key
            self._comment(
                ops,
                n,
                f"sbxloop could not finish: {reason}\n\n{_pr_ref(pr_number, pr_url)} passed "
                "the loop's own review and checks but GitHub would not let the loop land it. "
                "A human needs to look: merge or fix it by hand and close this issue, or "
                f"`!sbx retry {item.item_id}` once the cause is dealt with.",
            )
            self._remove_label(ops, n, self.labels.in_progress)
            self._add_label(ops, n, self.labels.blocked)
            return True

        return bool(self._guard("blocked report", go))

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
                f"Abandoned after retries: {error}\n\nRe-trigger by removing "
                f"`{self.labels.failed}` and re-adding `{self.labels.trigger}`.",
            )
            self._remove_label(ops, n, self.labels.in_progress)
            if not item.claimed:
                # Abandoned while still queued: the trigger label is what
                # is on the issue, and left there it would keep the item
                # polling as work (and make "re-add the trigger" a no-op).
                self._remove_label(ops, n, self.labels.trigger)
            self._add_label(ops, n, self.labels.failed)

        self._guard("abandon report", go)

    def report_cancelled(self, item: WorkItem, report: RunReport) -> None:
        def go(ops: GithubOps) -> None:
            n = item.source_key
            lines = _cancel_lines(report)
            if not report.requeued:
                # Neither failed nor triggered: the human decides what
                # happens next, so no label speaks for them. `!sbx retry`
                # is the reliable way back: re-adding the trigger label to an
                # unchanged issue is deduplicated by the store (same issue,
                # same content), so say so instead of promising it.
                lines.append(
                    f"To run it again from scratch: `!sbx retry {item.item_id}` in Discord "
                    f"(re-adding `{self.labels.trigger}` only re-runs it if the issue was "
                    "edited — an unchanged issue is deduplicated)."
                )
            self._comment(ops, n, "\n".join(lines))
            if not report.requeued:
                self._remove_label(ops, n, self.labels.in_progress)

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
