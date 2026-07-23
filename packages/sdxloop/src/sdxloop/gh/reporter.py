"""GithubReporterHook: mirrors run progress to a GitHub tracking issue.

The default consumer of the github-ops sandbox. When ``github.report_repo``
is configured, the hook opens a tracking issue at run start, comments as
tasks finish, and posts a final summary at run end. It never raises — a
reporting failure must not fail a run — and the EventBus isolates it anyway.
"""

from __future__ import annotations

import logging

from sdxloop.events import Event, HostEventTypes
from sdxloop.gh.ops import GithubOps, IssueRef

logger = logging.getLogger(__name__)


class GithubReporterHook:
    def __init__(self, ops: GithubOps, repo: str) -> None:
        self.ops = ops
        self.repo = repo
        self.issue: IssueRef | None = None
        self._task_lines: list[str] = []

    # -- Hook protocol -----------------------------------------------------

    def on_event(self, event: Event) -> None:
        try:
            if event.type == HostEventTypes.RUN_START:
                self._on_run_start(event)
            elif event.type == HostEventTypes.TASK_END:
                self._on_task_end(event)
            elif event.type == HostEventTypes.RUN_END:
                self._on_run_end(event)
        except Exception:
            logger.warning("github reporting failed for %s", event.type, exc_info=True)

    # -- handlers ----------------------------------------------------------

    def _on_run_start(self, event: Event) -> None:
        outcome = str(event.data.get("outcome", ""))
        body = (
            f"sdxloop run `{event.run_id}` started.\n\n"
            f"**Outcome:**\n\n> {outcome}\n\n"
            "Progress is reported as comments on this issue."
        )
        self.issue = self.ops.issue_create(
            self.repo,
            title=f"sdxloop run {event.run_id}",
            body=body,
            labels=["sdxloop"],
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

    def _on_run_end(self, event: Event) -> None:
        if self.issue is None:
            return
        state = event.data.get("state", "?")
        summary = "\n".join(self._task_lines) or "_no tasks were executed_"
        self.ops.issue_comment(
            self.repo,
            self.issue.number,
            f"Run `{event.run_id}` finished: **{state}**\n\n{summary}",
        )
