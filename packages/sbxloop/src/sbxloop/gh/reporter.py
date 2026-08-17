"""GithubReporterHook: mirrors run progress to a GitHub tracking issue.

The default consumer of the github-ops sandbox. When the GitHub integration
is configured (``[github].repo``) and reporting is enabled (``report = true``
or ``--report``), the engine opens a tracking issue right after the github
sandbox is ready, comments as tasks finish, and posts a final summary while
the sandbox is still alive.

Run start/end are **explicit calls** (``open_run``/``close_run``) rather
than bus events: the run lifecycle events are emitted outside the window in
which the hook is attached and the github sandbox exists, so subscribing to
them can never work — see #58. Only per-task progress arrives via the bus.

Every entry point is guarded: a reporting failure must not fail a run — and
the EventBus isolates ``on_event`` anyway.
"""

from __future__ import annotations

from sbxloop.events import Event, HostEventTypes
from sbxloop.gh.ops import GithubOps, IssueRef, PrRef
from sbxloop.log import get_logger

log = get_logger(__name__)


class GithubReporterHook:
    def __init__(self, ops: GithubOps, repo: str) -> None:
        self.ops = ops
        self.repo = repo
        self.issue: IssueRef | None = None
        self._task_lines: list[str] = []

    # -- explicit lifecycle (called by the engine, sandbox guaranteed alive) --

    def open_run(self, run_id: str, outcome: str) -> None:
        """Open (or on resume, re-find) the run's tracking issue."""
        try:
            self.issue = self._find_existing(run_id) or self._create(run_id, outcome)
        except Exception:
            log.warning("github reporting: opening tracking issue failed", exc_info=True)

    def close_run(self, run_id: str, state: str) -> None:
        """Post the final summary comment; call before sandbox teardown.

        A completed run also closes its issue — the record is finished, and
        an ever-growing pile of open "sbxloop run ..." issues drowns the
        signal. A failed (or interrupted-then-abandoned) run leaves its
        issue open as the thing that still needs a human; resume reuses it.
        """
        if self.issue is None:
            return
        summary = "\n".join(self._task_lines) or "_no tasks were executed_"
        try:
            self.ops.issue_comment(
                self.repo,
                self.issue.number,
                f"Run `{run_id}` finished: **{state}**\n\n{summary}",
            )
            if state == "completed":
                self.ops.raw(
                    "PATCH",
                    f"/repos/{self.repo}/issues/{self.issue.number}",
                    {"state": "closed", "state_reason": "completed"},
                )
        except Exception:
            log.warning("github reporting: final summary failed", exc_info=True)

    def note_delivery(self, run_id: str, pr: PrRef) -> None:
        """Refresh the tracking issue after an out-of-run delivery (``sbxloop
        deliver <run> --report``, #223): the issue was closed (or left open
        with a failed-delivery summary) when the run finished, so the PR
        link is appended as a comment and the issue closed as completed —
        the run's work is delivered, which is what "completed" promised.
        Same guard as the rest: reporting never fails the delivery."""
        if self.issue is None:
            return
        try:
            self.ops.issue_comment(
                self.repo,
                self.issue.number,
                f"Run `{run_id}` delivered: PR #{pr.number} {pr.url}",
            )
            self.ops.raw(
                "PATCH",
                f"/repos/{self.repo}/issues/{self.issue.number}",
                {"state": "closed", "state_reason": "completed"},
            )
        except Exception:
            log.warning("github reporting: delivery note failed", exc_info=True)

    # -- Hook protocol (task progress only) ----------------------------------

    def on_event(self, event: Event) -> None:
        try:
            if event.type == HostEventTypes.TASK_END:
                self._on_task_end(event)
        except Exception:
            log.warning("github reporting failed for %s", event.type, exc_info=True)

    # -- internals -----------------------------------------------------------

    def _find_existing(self, run_id: str) -> IssueRef | None:
        """A resumed run reuses its issue instead of opening a duplicate."""
        query = f'repo:{self.repo} is:issue in:title "sbxloop run {run_id}"'
        for item in self.ops.search_issues(query, per_page=5):
            if item.get("title") == f"sbxloop run {run_id}" and item.get("number"):
                return IssueRef(number=int(item["number"]), url=str(item.get("html_url", "")))
        return None

    def _create(self, run_id: str, outcome: str) -> IssueRef:
        body = (
            f"sbxloop run `{run_id}` started.\n\n"
            f"**Outcome:**\n\n> {outcome}\n\n"
            "Progress is reported as comments on this issue."
        )
        return self.ops.issue_create(
            self.repo,
            title=f"sbxloop run {run_id}",
            body=body,
            labels=["sbxloop"],
        )

    def _on_task_end(self, event: Event) -> None:
        if self.issue is None:
            return
        task_id = event.data.get("task_id", "?")
        title = event.data.get("title", "")
        state = event.data.get("state", "?")
        marker = "✅" if state == "done" else "❌"
        line = f"{marker} `{task_id}` {title} — **{state}**"
        self._task_lines.append(line)
        self.ops.issue_comment(self.repo, self.issue.number, line)
