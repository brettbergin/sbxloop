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
swallows :class:`GithubOpsError` and logs it. ``claim`` is the exception —
its result decides whether a run starts, so it returns False on failure.
"""

from __future__ import annotations

import hashlib
import logging
import re
import socket
import time
from collections.abc import Callable
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


class WorkSource(Protocol):
    name: str

    def poll(self) -> list[WorkItem]: ...
    def claim(self, item: WorkItem) -> bool: ...
    def report_started(self, item: WorkItem, run_id: str) -> None: ...
    def report_success(self, item: WorkItem, report: RunReport) -> None: ...
    def report_delivery_failed(self, item: WorkItem, report: RunReport) -> None: ...
    def report_retry(self, item: WorkItem, error: str, attempts_left: int) -> None: ...
    def report_abandoned(self, item: WorkItem, error: str) -> None: ...
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
        src = self._dir("running") / item.source_key
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
    ) -> None:
        self._ops = ops
        self.repo = repo
        self.labels = labels
        self.host = host or socket.gethostname()

    # -- helpers ----------------------------------------------------------------

    def _guard(self, what: str, fn: Callable[[GithubOps], Any]) -> Any:
        """Run a best-effort op; a GitHub failure is logged, never raised."""
        try:
            return fn(self._ops())
        except (GithubOpsError, WorkerError, SbxError):
            logger.warning("github source: %s failed for %s", what, self.repo, exc_info=True)
            return None

    def _issue_path(self, number: str) -> str:
        return f"/repos/{self.repo}/issues/{number}"

    def _add_label(self, ops: GithubOps, number: str, label: str) -> None:
        ops.raw("POST", f"{self._issue_path(number)}/labels", {"labels": [label]})

    def _remove_label(self, ops: GithubOps, number: str, label: str) -> None:
        try:
            ops.raw("DELETE", f"{self._issue_path(number)}/labels/{quote(label, safe='')}")
        except GithubOpsError as exc:
            # Already absent is fine (404 on the label resource).
            if "HTTP 404" not in str(exc):
                raise

    def _comment(self, ops: GithubOps, number: str, body: str) -> None:
        ops.issue_comment(self.repo, int(number), body)

    # -- protocol ---------------------------------------------------------------

    def poll(self) -> list[WorkItem]:
        query = f'repo:{self.repo} is:issue is:open label:"{self.labels.trigger}"'
        found = self._guard("search", lambda ops: ops.search_issues(query, per_page=50))
        items: list[WorkItem] = []
        for issue in found or []:
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
        """Re-verify (search lags), then swap trigger → in-progress and say so."""
        number = item.source_key
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
            self._remove_label(ops, number, self.labels.trigger)
            self._add_label(ops, number, self.labels.in_progress)
            self._comment(ops, number, f"sbxloop daemon claimed this issue (host `{self.host}`).")
            return True
        except (GithubOpsError, WorkerError, SbxError):
            logger.warning("github source: claim failed for #%s", number, exc_info=True)
            return False

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
            self._add_label(ops, n, self.labels.failed)

        self._guard("abandon report", go)

    def file_backlog(self, title: str, body: str, origin_run_id: str, *, trigger: bool) -> str:
        labels = [self.labels.trigger] if trigger else [self.labels.backlog]
        ref = self._ops().issue_create(
            self.repo,
            title,
            f"{body}\n\n---\nFiled by sbxloop run `{origin_run_id}`.",
            labels=labels,
        )
        return f"gh:{ref.number}"
