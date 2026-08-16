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
import logging
import re
import socket
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.errors import GithubOpsError, SbxError, WorkerError
from sbxloop.gh.ops import GithubOps

logger = logging.getLogger(__name__)

# A file still being written must not be claimed half-way; operators are
# told to write elsewhere and rename, but a small mtime guard costs nothing.
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
                    continue
                text = path.read_text()
            except OSError:
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
        except OSError:
            # Deleted by the operator, or taken by another instance.
            return False
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
            logger.warning("inbox: could not record result for %s", item.source_key, exc_info=True)

    def report_success(self, item: WorkItem, report: RunReport) -> None:
        self._finish(item, "done", _report_lines(report))

    def report_delivery_failed(self, item: WorkItem, report: RunReport) -> None:
        self._finish(item, "failed", _report_lines(report))

    def report_retry(self, item: WorkItem, error: str, attempts_left: int) -> None:
        # The file stays in running/ across retries; only the terminal move
        # records an outcome.
        logger.info(
            "inbox: %s failed (%s); %d attempt(s) left", item.source_key, error, attempts_left
        )

    def report_abandoned(self, item: WorkItem, error: str) -> None:
        self._finish(item, "failed", [f"Abandoned after retries: {error}"])

    def report_cancelled(self, item: WorkItem, report: RunReport) -> None:
        if report.requeued:
            # Still work: the file stays in running/, like a retry.
            logger.info("inbox: %s cancelled and re-queued", item.source_key)
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
            logger.warning("inbox: could not requeue %s", item.source_key, exc_info=True)

    def file_backlog(self, title: str, body: str, origin_run_id: str, *, trigger: bool) -> str:
        fingerprint = hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()[:8]
        name = f"{slugify(title)}-{fingerprint}.md"
        target = self._dir("pending" if trigger else "triage") / name
        target.write_text(f"# {title}\n\n{body}\n\n---\nFiled by sbxloop run {origin_run_id}\n")
        return f"inbox:{name}"


# -- github issues -----------------------------------------------------------------


class GitHubLabels:
    def __init__(self, trigger: str, in_progress: str, failed: str, backlog: str) -> None:
        self.trigger = trigger
        self.in_progress = in_progress
        self.failed = failed
        self.backlog = backlog


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
            logger.warning("github source: %s failed for %s", what, self.repo, exc_info=True)
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
        query = f'repo:{self.repo} is:issue is:open label:"{self.labels.trigger}"'
        try:
            found = self._ops().search_issues(query, per_page=50)
        except (GithubOpsError, WorkerError, SbxError) as exc:
            self._failed(exc)
            raise
        items: list[WorkItem] = []
        for issue in found:
            number = issue.get("number")
            if not number:
                continue
            items.append(
                WorkItem(
                    item_id=f"gh:{number}",
                    source="github",
                    source_key=str(number),
                    title=str(issue.get("title") or f"issue #{number}"),
                    body=str(issue.get("body") or ""),
                    url=str(issue.get("html_url") or ""),
                )
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
        added_in_progress = False
        comment_id: int | None = None
        try:
            ops = self._ops()
            issue = ops.raw("GET", self._issue_path(number))
            if not isinstance(issue, dict) or issue.get("state") != "open":
                return False
            names = {
                label.get("name") for label in issue.get("labels") or [] if isinstance(label, dict)
            }
            if self.labels.trigger not in names:
                return False
            epoch = self._trigger_epoch(ops, number)
            token = uuid.uuid4().hex
            self._comment(
                ops,
                number,
                f"{CLAIM_MARKER}{token} -->\n"
                f"sbxloop daemon claimed this issue (host `{self.host}`).",
            )
            comment_id, first_token = self._first_claim(ops, number, epoch, token)
            if first_token != token:
                logger.info(
                    "github source: lost the claim race for #%s (claim %s was first)",
                    number,
                    first_token,
                )
                self._delete_comment_quietly(number, comment_id)
                return False
            self._add_label(ops, number, self.labels.in_progress)
            added_in_progress = True
            self._remove_label(ops, number, self.labels.trigger)
        except (GithubOpsError, WorkerError, SbxError) as exc:
            logger.warning("github source: claim failed for #%s", number, exc_info=True)
            self._failed(exc)
            if added_in_progress:
                # Best-effort: leave the issue exactly as we found it.
                self._guard(
                    "claim rollback",
                    lambda ops: self._remove_label(ops, number, self.labels.in_progress),
                )
            self._delete_comment_quietly(number, comment_id)
            return False
        return True

    def _trigger_epoch(self, ops: GithubOps, number: str) -> str:
        """ISO timestamp of the trigger label's most recent addition — the
        start of the current claim cycle. Empty (every claim comment
        counts) if the issue's events do not show one."""
        latest = ""
        for events in self._pages(ops, f"{self._issue_path(number)}/events"):
            for event in events:
                if not isinstance(event, dict) or event.get("event") != "labeled":
                    continue
                label = event.get("label")
                if isinstance(label, dict) and label.get("name") == self.labels.trigger:
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
            self._comment(ops, n, "\n".join(_report_lines(report)))
            self._remove_label(ops, n, self.labels.in_progress)
            ops.raw("PATCH", self._issue_path(n), {"state": "closed", "state_reason": "completed"})

        self._guard("success report", go)

    def report_delivery_failed(self, item: WorkItem, report: RunReport) -> None:
        def go(ops: GithubOps) -> None:
            n = item.source_key
            lines = [
                *_report_lines(report),
                "",
                "The work completed but could not be delivered as a PR; a human needs "
                "to look. Re-trigger by removing the failed label and re-adding "
                f"`{self.labels.trigger}` (this will redo the work).",
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
                # unchanged issue is deduplicated by the store (same source
                # key, same content), so say so instead of promising it.
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
            # in-progress is the claim marker; a re-queued item is claimed
            # again without a fresh label swap. An abandoned item also
            # carries the failed label (absent → 404, tolerated).
            self._add_label(ops, n, self.labels.in_progress)
            self._remove_label(ops, n, self.labels.failed)
            self._comment(ops, n, f"Re-queued by {by}; a fresh run will start shortly.")

        self._guard("requeue report", go)

    def file_backlog(self, title: str, body: str, origin_run_id: str, *, trigger: bool) -> str:
        labels = [self.labels.trigger] if trigger else [self.labels.backlog]
        ref = self._ops().issue_create(
            self.repo,
            title,
            f"{body}\n\n---\nFiled by sbxloop run `{origin_run_id}`.",
            labels=labels,
        )
        return f"gh:{ref.number}"
