"""Work sources: where the daemon finds work and reports back.

Two implementations share one protocol. ``InboxSource`` watches a local
directory of ``.md`` files (claim = atomic rename between subdirectories).
``GitHubIssueSource`` polls the target repo for issues carrying the trigger
label and drives their lifecycle with labels and comments — every mutation
goes through the daemon's github-ops sandbox via :class:`GithubOps`, using
``raw.api`` for label add/remove and issue close (the same escape hatch the
run reporter already uses), so no new worker ops are needed.

Reporting is best-effort by construction: a GitHub hiccup while posting a
comment must never fail the daemon or lose an item, so every ``report_*``
swallows :class:`GithubOpsError` and logs it. Two exceptions: ``claim``'s
result decides whether a run starts, so it returns False on failure; and
``poll`` raises, so the loop can back off a source that is down instead of
mistaking an outage for an empty queue.
"""

from __future__ import annotations

import hashlib
import re
import socket
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple, Protocol
from urllib.parse import quote

from sbxloop.daemon.model import ItemKind, RunReport, WorkItem
from sbxloop.daemon.postmortem import postmortem_marker
from sbxloop.daemon.review import REVIEW_INSTRUCTIONS, collect_review
from sbxloop.engine.model import RunRecord
from sbxloop.errors import GithubOpsError, SbxError, WorkerError
from sbxloop.gh.ops import ChecksVerdict, GithubOps, SubmittedReview
from sbxloop.log import get_logger

log = get_logger(__name__)

# A file still being written must not be claimed half-way; operators are
# told to write elsewhere and rename, but a small mtime guard is free.
INBOX_SETTLE_S = 2.0
_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
# The claim comment doubles as the claim lock (see GitHubIssueSource.claim);
# this hidden marker is how competing daemons recognise each other's claims.
CLAIM_MARKER = "<!-- sbxloop-claim "
_CLAIM_RE = re.compile(re.escape(CLAIM_MARKER) + r"([0-9a-f]{32}) -->")
# GitHub list endpoints page at 100; an issue with more history than this
# many pages is not one the daemon should be arbitrating by comment anyway.
_MAX_PAGES = 10

# How much fetched review feedback one fix brief may carry. Enough for a
# review body plus a couple dozen inline comments; a brief is a prompt, not
# an archive.
_REVIEW_FEEDBACK_CLIP = 6_000


class WorkSource(Protocol):
    name: str

    def poll(self) -> list[WorkItem]: ...
    def claim(self, item: WorkItem) -> bool: ...
    def report_started(self, item: WorkItem, run_id: str) -> None: ...
    def report_success(self, item: WorkItem, report: RunReport) -> None: ...
    def report_delivery_failed(self, item: WorkItem, report: RunReport) -> None: ...
    def report_retry(self, item: WorkItem, error: str, attempts_left: int) -> None: ...
    def report_abandoned(self, item: WorkItem, error: str) -> None: ...
    def report_cancelled(self, item: WorkItem, report: RunReport) -> None: ...
    def report_requeued(self, item: WorkItem, by: str) -> None: ...
    def file_backlog(self, title: str, body: str, origin_run_id: str, *, trigger: bool) -> str: ...


def parse_markdown_item(text: str, fallback_title: str) -> tuple[str, str]:
    """(title, body): the first ``# heading`` is the title, the rest the body."""
    match = _HEADING_RE.search(text)
    if match is None:
        return fallback_title, text.strip()
    title = match.group(1).strip()
    body = (text[: match.start()] + text[match.end() :]).strip()
    return title, body


def slugify(title: str, limit: int = 48) -> str:
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")
    return (slug or "item")[:limit].rstrip("-")


def _report_lines(report: RunReport) -> list[str]:
    lines = [f"Run `{report.run_id}` finished: **{report.state}** — {report.task_summary}"]
    if report.tracking_issue is not None:
        number, url = report.tracking_issue
        lines.append(f"Tracking issue: #{number} {url}")
    if report.delivery is not None:
        number, url = report.delivery
        lines.append(f"Delivered as PR #{number}: {url}")
    if report.delivery_error:
        lines.append(f"Delivery failed: {report.delivery_error}")
    if report.filed:
        refs = ", ".join(f"#{ref.split(':', 1)[1]}" if ":" in ref else ref for ref in report.filed)
        lines.append(f"Filed: {refs}")
    if report.tool_filed:
        lines.append(f"Filed upstream (about sbxloop itself): {', '.join(report.tool_filed)}")
    if report.tool_noted:
        lines.append(
            "Findings about sbxloop itself, noted but not filed here (set `[daemon] "
            "tool_repo` to route them upstream): " + "; ".join(report.tool_noted)
        )
    if report.workspace:
        lines.append(f"Workspace: `{report.workspace}`")
    return lines


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


# -- inbox -----------------------------------------------------------------------


class InboxSource:
    """``root/pending`` in, ``running`` while claimed, ``done``/``failed``
    out (with a ``<name>.result.md`` beside the original), ``triage`` for
    agent-filed backlog awaiting a human."""

    name = "inbox"
    SUBDIRS = ("pending", "running", "done", "failed", "triage")

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.root = root
        self.clock = clock
        for sub in self.SUBDIRS:
            (root / sub).mkdir(parents=True, exist_ok=True)

    def _dir(self, sub: str) -> Path:
        return self.root / sub

    def poll(self) -> list[WorkItem]:
        items: list[WorkItem] = []
        now = self.clock()
        for path in sorted(self._dir("pending").glob("*.md")):
            try:
                if now - path.stat().st_mtime < INBOX_SETTLE_S:
                    log.debug("inbox.settling", path=str(path), settle_s=INBOX_SETTLE_S)
                    continue
                text = path.read_text()
            except OSError:
                log.warning("inbox.unreadable", path=str(path), exc_info=True)
                continue
            title, body = parse_markdown_item(text, path.stem)
            items.append(
                WorkItem(
                    item_id=f"inbox:{path.name}",
                    source="inbox",
                    source_key=path.name,
                    title=title,
                    body=body,
                )
            )
        return items

    def claim(self, item: WorkItem) -> bool:
        src = self._dir("pending") / item.source_key
        dst = self._dir("running") / item.source_key
        try:
            src.rename(dst)
        except OSError as exc:
            # Deleted by the operator, or taken by another instance.
            log.warning("inbox.claim_failed", item=item.item_id, path=str(src), error=str(exc))
            return False
        log.debug("inbox.claimed", item=item.item_id, path=str(dst))
        return True

    def report_started(self, item: WorkItem, run_id: str) -> None:
        return None

    def _finish(self, item: WorkItem, sub: str, lines: list[str]) -> None:
        # An item abandoned by an operator while still queued was never
        # claimed, so its file is still in pending/; left there it would
        # look like work still waiting, with its result note filed elsewhere.
        src = self._dir("running") / item.source_key
        if not src.exists():
            src = self._dir("pending") / item.source_key
        dst = self._dir(sub) / item.source_key
        try:
            if src.exists():
                src.replace(dst)
            (self._dir(sub) / f"{Path(item.source_key).stem}.result.md").write_text(
                "\n".join(lines) + "\n"
            )
        except OSError:
            log.warning("inbox.record_failed", item=item.item_id, target=str(dst), exc_info=True)

    def report_success(self, item: WorkItem, report: RunReport) -> None:
        self._finish(item, "done", _report_lines(report))

    def report_delivery_failed(self, item: WorkItem, report: RunReport) -> None:
        self._finish(item, "failed", _report_lines(report))

    def report_retry(self, item: WorkItem, error: str, attempts_left: int) -> None:
        # The file stays in running/ across retries; only the terminal move
        # records an outcome.
        log.info("inbox.retry", item=item.item_id, error=error, attempts_left=attempts_left)

    def report_abandoned(self, item: WorkItem, error: str) -> None:
        self._finish(item, "failed", [f"Abandoned after retries: {error}"])

    def report_cancelled(self, item: WorkItem, report: RunReport) -> None:
        if report.requeued:
            # Still work: the file stays in running/, like a retry.
            log.info("inbox.cancelled_requeued", item=item.item_id)
            return
        self._finish(item, "failed", _cancel_lines(report))

    def report_requeued(self, item: WorkItem, by: str) -> None:
        # Undo the terminal move so the file is back where a running item lives,
        # and drop the old result note with it: left behind, a later success
        # would show the same item as both failed and done.
        src = self._dir("failed") / item.source_key
        note = self._dir("failed") / f"{Path(item.source_key).stem}.result.md"
        try:
            if src.exists():
                src.replace(self._dir("running") / item.source_key)
                note.unlink(missing_ok=True)
        except OSError:
            log.warning("inbox.requeue_failed", item=item.item_id, path=str(src), exc_info=True)

    def file_backlog(self, title: str, body: str, origin_run_id: str, *, trigger: bool) -> str:
        fingerprint = hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()[:8]
        name = f"{slugify(title)}-{fingerprint}.md"
        target = self._dir("pending" if trigger else "triage") / name
        target.write_text(f"# {title}\n\n{body}\n\n---\nFiled by sbxloop run {origin_run_id}\n")
        return f"inbox:{name}"

    def enqueue(self, title: str, body: str, *, by: str) -> str:
        """Queue a new work item on behalf of a human (the concierge's write
        path): a pending ``.md`` the next poll picks up. The fingerprint
        includes the clock, so asking twice queues twice — that is what
        was asked; ``!sbx abandon`` undoes it."""
        stamp = f"{title}\n{body}\n{self.clock():.3f}"
        fingerprint = hashlib.sha256(stamp.encode()).hexdigest()[:8]
        name = f"{slugify(title)}-{fingerprint}.md"
        target = self._dir("pending") / name
        target.write_text(f"# {title}\n\n{body}\n\n---\nRequested by {by} via the concierge\n")
        log.info("inbox.enqueued", item=f"inbox:{name}", by=by, title=title[:80])
        return f"inbox:{name}"


# -- github issues -----------------------------------------------------------------


class GitHubLabels:
    def __init__(
        self,
        trigger: str,
        in_progress: str,
        failed: str,
        backlog: str,
        delivered: str = "sbxloop:delivered",
        audit: str = "sbxloop:audit",
        completed: str = "sbxloop:completed",
    ) -> None:
        self.trigger = trigger
        self.in_progress = in_progress
        self.failed = failed
        self.backlog = backlog
        self.delivered = delivered
        # The discovery lane's trigger: an issue carrying it is a charter to
        # investigate and file findings, not a change to deliver.
        self.audit = audit
        # The durable "sbxloop did this" mark, applied when the work lands:
        # at merge for patch items, at close for audits.
        self.completed = completed

    def trigger_for(self, kind: str) -> str:
        """The label that put an item of ``kind`` in the queue."""
        return self.audit if kind == "audit" else self.trigger


class PrSnapshot(NamedTuple):
    """One poll's view of a delivered PR — everything the gates consult."""

    checks: ChecksVerdict
    review: str  # APPROVED | CHANGES_REQUESTED | NONE
    merged: bool
    state: str  # open | closed
    # The branch head this poll saw; "" when it could not be read (and for
    # merged/closed PRs, whose snapshot skips the read). Feeds the takeover
    # guard (#412).
    head_sha: str = ""


class GitHubIssueSource:
    """Issues in the target repo carrying the trigger label are work.

    ``ops`` is a zero-arg provider (``DaemonGithub.ops``) rather than a
    fixed :class:`GithubOps`: the daemon may re-provision its sandbox at
    any time and the source must follow.

    A patch item's source issue settles when its PR *merges* — close plus
    ``labels.completed`` (see :meth:`report_merged`) — never at acceptance,
    where it only gains ``labels.delivered``. ``close_on_success`` used to
    close at acceptance and is now accepted but ignored.
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
        close_on_success: bool = True,
        tool_repo: str | None = None,
    ) -> None:
        self._ops = ops
        self.repo = repo
        self.labels = labels
        self.host = host or socket.gethostname()
        # Deprecated: kept so existing wiring still constructs, but the
        # issue now settles on merge regardless (see report_merged).
        self.close_on_success = close_on_success
        # Findings ABOUT sbxloop go to its own tracker (never the project's);
        # unset means "note them in the closing comment only".
        self.tool_repo = tool_repo
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
        items: list[WorkItem] = []
        seen: set[str] = set()
        # Two lanes, two labels, one queue: patch items (trigger label)
        # deliver PRs; audit items (audit label) investigate and file
        # issues. An issue carrying both is a patch — the safer reading.
        lanes: tuple[tuple[str, ItemKind], ...] = (
            (self.labels.trigger, "patch"),
            (self.labels.audit, "audit"),
        )
        started = time.monotonic()
        for label, kind in lanes:
            query = f'repo:{self.repo} is:issue is:open label:"{label}"'
            log.debug("github.poll_start", repo=self.repo, lane=kind, label=label)
            try:
                found = self._ops().search_issues(query, per_page=50)
            except (GithubOpsError, WorkerError, SbxError) as exc:
                log.warning(
                    "github.poll_failed",
                    repo=self.repo,
                    lane=kind,
                    label=label,
                    duration_s=round(time.monotonic() - started, 2),
                    error=str(exc),
                )
                self._failed(exc)
                raise
            log.debug(
                "github.poll_lane",
                repo=self.repo,
                lane=kind,
                label=label,
                issues=len(found),
                duration_s=round(time.monotonic() - started, 2),
            )
            for issue in found:
                number = issue.get("number")
                if not number or str(number) in seen:
                    continue
                seen.add(str(number))
                items.append(
                    WorkItem(
                        item_id=f"gh:{number}",
                        source="github",
                        source_key=str(number),
                        kind=kind,
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
        trigger = self.labels.trigger_for(item.kind)
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
            stale_delivered = self.labels.delivered in names
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
        # A re-triggered issue may still carry the delivered label from a
        # rejected PR; it is stale the moment a new run is claimed. Cleared
        # regardless of the current mode: the label was written by whatever
        # mode was configured *then*, and a daemon restarted with
        # close_on_success back on would otherwise leave it beside
        # in-progress. Best-effort — a leftover label must not un-claim.
        # Only when the re-GET actually showed it: a blind DELETE 404s on
        # every fresh issue and the event stream renders each as an error
        # panel (field noise on every audit claim).
        if stale_delivered:
            self._guard(
                "clear delivered label",
                lambda ops: self._remove_label(ops, number, self.labels.delivered),
            )
        log.info(
            "github.claimed",
            item=item.item_id,
            repo=self.repo,
            stale_delivered_cleared=stale_delivered,
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

    def report_success(self, item: WorkItem, report: RunReport) -> None:
        def go(ops: GithubOps) -> None:
            n = item.source_key
            lines = _report_lines(report)
            # An audit is a chore whose output is the issues it filed: it
            # closes on completion — there is no PR whose merge could settle
            # it. A patch item's issue settles when its PR merges, so here
            # it stays open wearing the delivered label.
            close = item.kind == "audit"
            if item.kind == "audit" and not report.filed:
                lines.append("The audit filed no findings.")
            if not close:
                lines.append(
                    "Leaving this issue open; it will be closed when the PR merges. "
                    f"If the PR is rejected, re-add `{self.labels.trigger}` to run "
                    "it again."
                )
            self._comment(ops, n, "\n".join(lines))
            self._remove_label(ops, n, self.labels.in_progress)
            if close:
                self._add_label(ops, n, self.labels.completed)
                ops.raw(
                    "PATCH", self._issue_path(n), {"state": "closed", "state_reason": "completed"}
                )
            else:
                # Delivered is added *after* in-progress is removed so a
                # failure between the two leaves the issue merely un-labeled,
                # never carrying two lifecycle labels at once.
                self._add_label(ops, n, self.labels.delivered)

        self._guard("success report", go)

    def report_merged(self, item: WorkItem, pr_number: int, pr_url: str) -> bool:
        """The delivered PR merged: the work landed. Close the source issue
        and leave ``labels.completed`` as the durable mark.

        Returns True only when every step succeeded, so the caller retries
        an interrupted settle instead of recording it as done. Labels come
        before the close: a failure mid-way leaves an open, correctly
        labelled issue rather than a closed one with no mark.
        """

        def go(ops: GithubOps) -> bool:
            n = item.source_key
            ref = f"[PR #{pr_number}]({pr_url})" if pr_url else f"PR #{pr_number}"
            self._comment(ops, n, f"{ref} was merged — work completed by sbxloop; closing.")
            self._remove_label(ops, n, self.labels.in_progress)
            self._remove_label(ops, n, self.labels.delivered)
            self._add_label(ops, n, self.labels.completed)
            # Blind PATCH, no state pre-read: the PR body's `Closes #N` may
            # have closed the issue already, and re-closing a closed issue
            # is a no-op success.
            ops.raw("PATCH", self._issue_path(n), {"state": "closed", "state_reason": "completed"})
            return True

        return bool(self._guard("merge report", go))

    def report_pr_closed(self, item: WorkItem, pr_number: int, pr_url: str) -> bool:
        """The delivered PR was closed without merging — a human rejected
        it. The issue stays open, marked failed, for the human to decide."""

        def go(ops: GithubOps) -> bool:
            n = item.source_key
            ref = f"[PR #{pr_number}]({pr_url})" if pr_url else f"PR #{pr_number}"
            self._comment(
                ops,
                n,
                f"{ref} was closed without being merged. Marking this failed — "
                f"re-add `{self.labels.trigger}` to try again, or close this "
                "issue if the work is no longer wanted.",
            )
            self._remove_label(ops, n, self.labels.delivered)
            self._remove_label(ops, n, self.labels.in_progress)
            self._add_label(ops, n, self.labels.failed)
            return True

        return bool(self._guard("pr-closed report", go))

    def report_delivery_failed(self, item: WorkItem, report: RunReport) -> None:
        def go(ops: GithubOps) -> None:
            n = item.source_key
            lines = [
                *_report_lines(report),
                "",
                "The work completed but could not be delivered as a PR; a human needs "
                "to look. Re-trigger by removing the failed label and re-adding "
                f"`{self.labels.trigger_for(item.kind)}` (this will redo the work).",
            ]
            self._comment(ops, n, "\n".join(lines))
            self._remove_label(ops, n, self.labels.in_progress)
            self._add_label(ops, n, self.labels.failed)

        self._guard("delivery-failure report", go)

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
                f"`{self.labels.failed}` and re-adding `{self.labels.trigger_for(item.kind)}`.",
            )
            self._remove_label(ops, n, self.labels.in_progress)
            if not item.claimed:
                # Abandoned while still queued: the trigger label is what
                # is on the issue, and left there it would keep the item
                # polling as work (and make "re-add the trigger" a no-op).
                self._remove_label(ops, n, self.labels.trigger_for(item.kind))
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
                # unchanged issue is deduplicated by the store (same source
                # key, same content), so say so instead of promising it.
                lines.append(
                    f"To run it again from scratch: `!sbx retry {item.item_id}` in Discord "
                    f"(re-adding `{self.labels.trigger_for(item.kind)}` only re-runs it if "
                    "the issue was edited — an unchanged issue is deduplicated)."
                )
            self._comment(ops, n, "\n".join(lines))
            if not report.requeued:
                self._remove_label(ops, n, self.labels.in_progress)

        self._guard("cancel report", go)

    def report_requeued(self, item: WorkItem, by: str) -> None:
        def go(ops: GithubOps) -> None:
            n = item.source_key
            # An abandoned item carries the failed label and a done item
            # (keep-open mode, in whatever mode wrote it) the delivered
            # label — both describe the previous run, not the one about to
            # start (absent → 404, tolerated). Strip them *before* adding
            # in-progress: `_guard` swallows a failure mid-way, and adding
            # first would leave the issue wearing two lifecycle labels.
            self._remove_label(ops, n, self.labels.failed)
            self._remove_label(ops, n, self.labels.delivered)
            self._remove_label(ops, n, self.labels.completed)
            # in-progress is the claim marker; a re-queued item is claimed
            # again without a fresh label swap.
            self._add_label(ops, n, self.labels.in_progress)
            self._comment(ops, n, f"Re-queued by {by}; a fresh run will start shortly.")

        self._guard("requeue report", go)

    def audit_issue_state(self, title: str, since_iso: str) -> tuple[bool, bool]:
        """(an OPEN issue with this exact title exists, one was CREATED since
        ``since_iso``) — GitHub is the source of truth for the schedule, so a
        wiped state dir cannot double-file and a still-open audit is never
        re-opened on top of itself. Raises on GitHub failure (caller skips)."""
        ops = self._ops()
        quoted = title.replace('"', "")
        opened = ops.search_issues(
            f'repo:{self.repo} is:issue is:open label:"{self.labels.audit}" "{quoted}" in:title',
            per_page=5,
        )
        recent = ops.search_issues(
            f'repo:{self.repo} is:issue "{quoted}" in:title created:>={since_iso}', per_page=5
        )

        def exact(rows: list[dict[str, Any]]) -> bool:
            return any(str(r.get("title") or "") == title for r in rows)

        log.debug(
            "github.audit_issue_state",
            repo=self.repo,
            title=title,
            open_matches=len(opened),
            recent_matches=len(recent),
        )
        return exact(opened), exact(recent)

    def _filed(self, kind: str, ref: str, **fields: Any) -> str:
        log.info("github.issue_filed", kind=kind, ref=ref, **fields)
        return ref

    def file_audit(self, title: str, body: str) -> str:
        ref = self._ops().issue_create(self.repo, title, body, labels=[self.labels.audit])
        return self._filed("audit", f"gh:{ref.number}", repo=self.repo, title=title)

    def file_postmortem(self, item: WorkItem, dossier: str, run_id: str) -> str:
        """Open a post-mortem as an audit-lane charter and return its ref.

        Labelled with the audit label so the daemon picks it up like any
        other charter; the marker lets the store (and a human) tie it back
        to the run it dissects."""
        ref = self._ops().issue_create(
            self.repo,
            f"post-mortem: {' '.join(item.title.split())[:80]} (run {run_id})",
            f"{dossier}\n\n{postmortem_marker(run_id)}\n---\nFiled by the sbxloop daemon "
            f"after `{item.item_id}` failed (run `{run_id}`).",
            labels=[self.labels.audit],
        )
        return self._filed(
            "post-mortem", f"gh:{ref.number}", repo=self.repo, item=item.item_id, run=run_id
        )

    def file_tool_backlog(self, title: str, body: str, origin_run_id: str) -> str | None:
        """A finding about the TOOL, filed to ``tool_repo`` (None → caller notes it)."""
        if not self.tool_repo:
            return None
        ref = self._ops().issue_create(
            self.tool_repo,
            title,
            f"{body}\n\n---\nFiled by sbxloop run `{origin_run_id}` while working on "
            f"`{self.repo}` (a finding about the tool, routed upstream).",
            labels=[self.labels.backlog],
        )
        return self._filed(
            "tool-backlog",
            f"{self.tool_repo}#{ref.number}",
            repo=self.tool_repo,
            run=origin_run_id,
            title=title,
        )

    def pr_state(self, pr_number: int) -> PrSnapshot:
        """Everything the acceptance gate needs to know about a PR.

        Up to three reads rather than one: the check runs hang off the
        *head commit*, so the sha has to be fetched first, and fetching it
        fresh each poll is what makes a PR that was pushed to since the
        last look report on its new commit rather than the old one's
        checks. A merged or closed PR is past checks and review, so those
        two reads are skipped and their fields are placeholders the gate
        never consults.
        """
        pr = self._ops().pr_get(self.repo, pr_number)
        merged = bool(pr.get("merged"))
        state = str(pr.get("state") or "")
        if merged or state == "closed":
            return PrSnapshot(ChecksVerdict("pending", 0, (), ()), "NONE", merged, state)
        sha = str(((pr.get("head") or {}).get("sha")) or "")
        checks = (
            self._ops().pr_checks(self.repo, sha)
            if sha
            # No head sha is not "green": it is an answer we could not get,
            # and the gate must not read that as permission to merge.
            else ChecksVerdict("pending", 0, ("unknown head commit",), ())
        )
        return PrSnapshot(
            checks, self._ops().pr_review_state(self.repo, pr_number), merged, state, head_sha=sha
        )

    def pr_merge_state(self, pr_number: int) -> tuple[bool, str]:
        """(merged, open/closed state) for a delivered PR.

        Raises on failure — the merge watch throttles and retries; a
        swallowed error would read as "still open" and silently postpone
        the settle.
        """
        pr = self._ops().pr_get(self.repo, pr_number)
        return bool(pr.get("merged")), str(pr.get("state") or "")

    def pr_review_feedback(self, pr_number: int) -> str:
        """The objections standing on a PR, as text a fix round can act on.

        The fix agent's sandbox holds no GitHub credential (#437), so the
        daemon reads the change-requesting review bodies and the inline
        review comments through the github-ops sandbox and bakes them into
        the fix brief. Latest verdict per reviewer only, matching how
        ``fold_reviews`` judges the PR; inline comments are quoted with
        their anchors so the fix agent can find the lines.
        """
        ops = self._ops()
        latest: dict[str, dict[str, object]] = {}
        for review in ops.raw("GET", f"/repos/{self.repo}/pulls/{pr_number}/reviews") or []:
            login = str(((review.get("user") or {}).get("login")) or "")
            if str(review.get("state") or "") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[login] = review
        parts: list[str] = []
        for review in latest.values():
            if str(review.get("state")) != "CHANGES_REQUESTED":
                continue
            body = str(review.get("body") or "").strip()
            if body:
                parts.append(body)
        for comment in ops.raw("GET", f"/repos/{self.repo}/pulls/{pr_number}/comments") or []:
            body = str(comment.get("body") or "").strip()
            if not body:
                continue
            path = str(comment.get("path") or "")
            line = comment.get("line") or comment.get("original_line")
            anchor = f"`{path}:{line}`: " if path and line else f"`{path}`: " if path else ""
            parts.append(f"- {anchor}{body}")
        return "\n\n".join(parts)[:_REVIEW_FEEDBACK_CLIP]

    def post_review(
        self, run: RunRecord, pr_number: int, origin_run_id: str
    ) -> SubmittedReview | None:
        """Post a finished review run's verdict to the PR it reviewed.

        The counterpart to :meth:`file_review`, which queues the work: this
        is where its output lands. On the PR, not in the tracker — filing
        review findings as issues is the behaviour being replaced.
        """
        return collect_review(
            run,
            ops=self._ops(),
            repo=self.repo,
            pr_number=pr_number,
            origin_run_id=origin_run_id,
        )

    def file_review(self, item: WorkItem, pr_number: int, pr_url: str, run_id: str) -> str:
        """Open a review of a PR the loop just delivered, as an audit charter:
        the loop evaluating the code it wrote."""
        body = (
            f"# Review: PR #{pr_number} (delivered by run `{run_id}` for {item.item_id})\n\n"
            f"PR: {pr_url}\nSource issue: {item.url or item.source_key}\n\n"
            "Charter: review this PR as a skeptical maintainer. The source issue's text is "
            "quoted in this charter, and the workspace is a fresh clone — check out the PR's "
            "branch, and read the full diff with git, not `gh` (`gh` is not authenticated in "
            "this sandbox): `git fetch origin <base>` then `git diff origin/<base>...HEAD`. "
            "Look for: defects and wrong behaviour, missing edge cases and tests, scope "
            "drift from the issue, unjustified claims in the PR body, style that contradicts "
            "the repository's conventions, and anything a reviewer would block on.\n\n"
            f"{REVIEW_INSTRUCTIONS}\n\n"
            f"<!-- sbxloop-review {run_id} -->"
        )
        ref = self._ops().issue_create(
            self.repo,
            f"review: PR #{pr_number} — {' '.join(item.title.split())[:70]} (run {run_id})",
            body,
            labels=[self.labels.audit],
        )
        return self._filed(
            "review",
            f"gh:{ref.number}",
            repo=self.repo,
            item=item.item_id,
            run=run_id,
            pr=pr_number,
        )

    def file_backlog(self, title: str, body: str, origin_run_id: str, *, trigger: bool) -> str:
        labels = [self.labels.trigger] if trigger else [self.labels.backlog]
        ref = self._ops().issue_create(
            self.repo,
            title,
            f"{body}\n\n---\nFiled by sbxloop run `{origin_run_id}`.",
            labels=labels,
        )
        return self._filed(
            "backlog",
            f"gh:{ref.number}",
            repo=self.repo,
            run=origin_run_id,
            title=title,
            trigger=trigger,
        )
