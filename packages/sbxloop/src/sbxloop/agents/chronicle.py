"""A run's chronicle: its events, told in its channel by the agents working it.

A run reports through events, which is the right shape for a log and the
wrong shape for a conversation. This turns the few events a person actually
waits on into short posts under the name of the agent that did the work:
the plan from the planner, each finished task from its agent, the verdict
from the critic, a steering reply from the agent that was asked, and the
delivery or the notice from the lead.

It is deliberately quiet. Progress is coalesced to one post per interval and
the whole run is capped by ``[agent_team] max_posts_per_run``, so a long run
does not bury the conversation it is happening in. What ends a run - its
delivery, and a notice when it stopped - is posted whatever the cap says,
because a run nobody hears finish is a run nobody can act on. Every post
carries a key naming the moment it is about, so a resumed or replayed run
says each thing once.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sbxloop.agents.assignment import BUILTIN_FOR_ROLE, AgentAssignment
from sbxloop.agents.builtin import ANGIE_SLUG
from sbxloop.agents.posts import (
    TERMINAL_POST_KINDS,
    ArtifactRef,
    ChannelPost,
    ChannelPoster,
    PostKind,
    RunArtifacts,
)
from sbxloop.config import Config
from sbxloop.daemon.model import WorkItem
from sbxloop.events import HostEventTypes
from sbxloop.log import get_logger
from sbxloop_worker.protocol import Event

log = get_logger(__name__)

#: The phase whose agent does a task's work, by run kind.
_WORKING_PHASE: dict[str, str] = {"code": "build", "workload": "operator_execute"}
#: How a run ending in one of these states reads: it delivered something.
_DELIVERED_STATES = frozenset({"merged", "completed", "published"})
#: One post's text is cut to this; a channel is not a log.
_MAX_TEXT = 500


def _clip(text: str, limit: int = _MAX_TEXT) -> str:
    flat = text.strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


class RunChronicle:
    """A run-bus subscriber that posts a run's story into its channel."""

    def __init__(
        self,
        poster: ChannelPoster,
        assignment: AgentAssignment | None,
        item: WorkItem,
        config: Config,
        clock: Callable[[], float],
        *,
        artifacts: RunArtifacts | None = None,
    ) -> None:
        self.poster = poster
        self.assignment = assignment
        self.item = item
        self.config = config
        self.clock = clock
        self.artifacts = artifacts
        self.channel_id = str(item.channel_id or "")
        #: The roster the run announced, and how many tasks have ended.
        self._total = 0
        self._done = 0
        #: Posts made (a post the channel already had counts: a replay must
        #: reach the same cap as the run it replays).
        self._posted = 0
        self._last_progress: float | None = None

    @classmethod
    def for_item(
        cls,
        poster: ChannelPoster | None,
        assignment: AgentAssignment | None,
        item: WorkItem,
        config: Config,
        clock: Callable[[], float],
        *,
        artifacts: RunArtifacts | None = None,
    ) -> RunChronicle | None:
        """The chronicle for ``item``, or None when there is nobody to tell:
        no channel asked for the work, or this daemon has no poster."""
        if poster is None or not item.channel_id or config.agent_team.chronicle == "off":
            return None
        return cls(poster, assignment, item, config, clock, artifacts=artifacts)

    # -- the bus -----------------------------------------------------------

    def on_event(self, event: Event) -> None:
        """Best-effort: a channel that cannot be written to never fails a run."""
        try:
            self._handle(event)
        except Exception:
            log.warning(
                "chronicle.post_failed", run=event.run_id, event_type=event.type, exc_info=True
            )

    def _handle(self, event: Event) -> None:
        data = event.data
        if event.type == HostEventTypes.RUN_TASKS:
            self._roster(event, data)
        elif event.type == HostEventTypes.TASK_END:
            self._task_end(event, data)
        elif event.type == HostEventTypes.REVIEW_VERDICT:
            self._review(event, data)
        elif event.type == HostEventTypes.CHAT_REPLY:
            self._reply(event, data)
        elif event.type in (HostEventTypes.RUN_MERGED, HostEventTypes.RUN_PUBLISHED):
            self._delivery(event, data)
        elif event.type == HostEventTypes.RUN_END:
            self._run_end(event, data)
        elif event.type in (HostEventTypes.RUN_BLOCKED, HostEventTypes.RUN_GATED):
            self._notice(event, data)

    # -- each kind ---------------------------------------------------------

    def _roster(self, event: Event, data: dict[str, Any]) -> None:
        tasks = data.get("tasks")
        count = len(tasks) if isinstance(tasks, Sequence) else 0
        self._total = max(self._total, count)
        if count:
            self._post(
                event,
                "plan",
                f"Split the ask into {_plural(count, 'task')}",
                dedupe=f"{event.run_id}:plan",
                agent=self._agent_for("decompose"),
            )

    def _task_end(self, event: Event, data: dict[str, Any]) -> None:
        task_id = str(data.get("task_id") or "")
        self._done += 1
        total = max(self._total, self._done)
        now = self.clock()
        interval = self.config.agent_team.progress_interval_s
        if self._last_progress is not None and now - self._last_progress < interval:
            return
        title = str(data.get("title") or task_id or "the task")
        posted = self._post(
            event,
            "progress",
            f"Finished task {self._done} of {total}: {title}",
            dedupe=f"{event.run_id}:progress:{task_id or self._done}",
            agent=self._task_agent(data, task_id),
            task_id=task_id or None,
        )
        if posted:
            self._last_progress = now

    def _review(self, event: Event, data: dict[str, Any]) -> None:
        findings = int(data.get("findings") or 0)
        blocking = int(data.get("blocking") or 0)
        if findings == 0:
            text = "Reviewed: no findings"
        elif blocking:
            text = f"{_plural(findings, 'finding')}, {blocking} blocking"
        else:
            text = _plural(findings, "finding")
        self._post(
            event,
            "review",
            text,
            dedupe=f"{event.run_id}:review:{data.get('round') or 1}",
            agent=self._agent_for("review"),
        )

    def _reply(self, event: Event, data: dict[str, Any]) -> None:
        reply = str(data.get("reply") or "").strip()
        if not reply:
            return
        message_id = str(data.get("message_id") or self._done)
        self._post(
            event,
            "reply",
            reply,
            dedupe=f"{event.run_id}:reply:{message_id}",
            agent=self._agent_for("steer", data),
        )

    def _delivery(self, event: Event, data: dict[str, Any]) -> None:
        artifacts = self._run_artifacts(event.run_id)
        self._post(
            event,
            "delivery",
            self._delivery_text(data, artifacts),
            dedupe=f"{event.run_id}:delivery",
            agent=self._lead(),
            artifacts=artifacts,
        )

    def _run_end(self, event: Event, data: dict[str, Any]) -> None:
        state = str(data.get("state") or "")
        if state in _DELIVERED_STATES:
            self._delivery(event, data)
            return
        reason = str(data.get("reason") or "").strip()
        text = f"Run ended: {state or 'stopped'}." + (f" {reason}" if reason else "")
        self._post(
            event,
            "notice",
            text,
            dedupe=f"{event.run_id}:notice:{state or 'end'}",
            agent=self._lead(),
        )

    def _notice(self, event: Event, data: dict[str, Any]) -> None:
        if event.type == HostEventTypes.RUN_GATED:
            state = "gated"
            pr = data.get("pr")
            text = (
                f"Ready to merge pull request #{pr}; waiting for a human approval."
                if pr
                else "Ready to merge; waiting for a human approval."
            )
        else:
            state = "blocked"
            why = str(data.get("why") or "").strip()
            text = f"Blocked: {why}" if why else "Blocked; a person needs to look."
        self._post(
            event,
            "notice",
            text,
            dedupe=f"{event.run_id}:notice:{state}",
            agent=self._lead(),
        )

    # -- text and files ----------------------------------------------------

    def _delivery_text(self, data: dict[str, Any], artifacts: tuple[ArtifactRef, ...]) -> str:
        url = str(data.get("url") or "").strip()
        if url:
            pr = data.get("pr")
            label = f"pull request #{pr}" if pr else "the pull request"
            return f"Merged {label}: {url}"
        message = str(data.get("message") or "").strip()
        if message:
            return message
        if artifacts:
            return "Delivered " + ", ".join(ref.relpath for ref in artifacts)
        return "Finished."

    def _run_artifacts(self, run_id: str) -> tuple[ArtifactRef, ...]:
        if self.artifacts is None:
            return ()
        try:
            return tuple(self.artifacts.artifacts_for_run(run_id))
        except Exception:
            log.warning("chronicle.artifacts_failed", run=run_id, exc_info=True)
            return ()

    # -- who speaks --------------------------------------------------------

    def _lead(self) -> str:
        binding = self.assignment.lead_binding() if self.assignment else None
        if binding is not None:
            return binding.slug
        return self.assignment.lead if self.assignment else ANGIE_SLUG

    def _agent_for(self, phase: str, data: dict[str, Any] | None = None) -> str:
        stamped = (data or {}).get("agent_slug")
        if isinstance(stamped, str) and stamped:
            return stamped
        binding = self.assignment.binding_for(phase) if self.assignment else None
        if binding is not None:
            return binding.slug
        from sbxloop.engine.harness import ROLE_BY_PHASE

        return BUILTIN_FOR_ROLE.get(ROLE_BY_PHASE.get(phase, ""), ANGIE_SLUG)

    def _task_agent(self, data: dict[str, Any], task_id: str) -> str:
        stamped = data.get("agent_slug")
        if isinstance(stamped, str) and stamped:
            return stamped
        phase = _WORKING_PHASE.get(self.item.kind)
        if phase is None:
            return self._lead()
        binding = self.assignment.binding_for(phase, task_id or None) if self.assignment else None
        return binding.slug if binding is not None else self._agent_for(phase)

    # -- posting -----------------------------------------------------------

    def _post(
        self,
        event: Event,
        kind: PostKind,
        text: str,
        *,
        dedupe: str,
        agent: str,
        task_id: str | None = None,
        artifacts: tuple[ArtifactRef, ...] = (),
    ) -> bool:
        """Hand one post to the poster, unless this run has said enough."""
        settings = self.config.agent_team
        terminal = kind in TERMINAL_POST_KINDS
        if settings.chronicle == "off":
            return False
        if not terminal and settings.chronicle == "quiet":
            return False
        if not terminal and self._posted >= settings.max_posts_per_run:
            return False
        message_id = self.poster.post(
            ChannelPost(
                channel_id=self.channel_id,
                author_agent=agent,
                kind=kind,
                text=_clip(text),
                run_id=event.run_id,
                item_id=self.item.item_id,
                dedupe_key=dedupe,
                task_id=task_id,
                artifacts=artifacts,
            )
        )
        if message_id is None:
            return False
        self._posted += 1
        return True


__all__ = ["RunChronicle"]
