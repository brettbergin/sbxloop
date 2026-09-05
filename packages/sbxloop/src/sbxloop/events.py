"""Host-side event plumbing: a synchronous pub/sub bus over protocol Events."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from sbxloop.cli.cmdfmt import format_command
from sbxloop.log import get_logger
from sbxloop_worker.protocol import Event, EventTypes

log = get_logger(__name__)

Subscriber = Callable[[Event], None]


@runtime_checkable
class Hook(Protocol):
    """Extension point: receives every event published on the bus.

    Exceptions are contained: ``EventBus.publish`` catches and logs anything
    a hook raises, so a misbehaving hook can never break a run and hook
    authors need no defensive try/except of their own. Hooks run
    synchronously on the publishing thread, though, so keep them fast.
    """

    def on_event(self, event: Event) -> None: ...


class EventBus:
    """Synchronous fan-out of protocol events to subscribers.

    Subscriber exceptions are isolated: they are logged and swallowed so
    telemetry consumers can never fail a run.

    Publishing is thread-safe: parallel provisioning/install threads emit
    concurrently, and the lock keeps subscriber invocations serialized so
    consumers that are not thread-safe themselves (the SQLite persister,
    console printers) never see two events at once. Reentrant (a subscriber
    may emit) — hooks still run synchronously on the publishing thread.
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._publish_lock = threading.RLock()

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        """Register a subscriber; returns an unsubscribe callable."""
        self._subscribers.append(fn)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(fn)

        return unsubscribe

    def attach_hook(self, hook: Hook) -> Callable[[], None]:
        return self.subscribe(hook.on_event)

    def publish(self, event: Event) -> None:
        with self._publish_lock:
            for fn in list(self._subscribers):
                try:
                    fn(event)
                except Exception:
                    log.exception(
                        "events.subscriber_failed",
                        subscriber=repr(fn),
                        event_type=event.type,
                        run=event.run_id,
                    )

    def emit(
        self,
        type: str,
        run_id: str,
        job_id: str | None = None,
        **data: Any,
    ) -> Event:
        """Construct an Event stamped now, publish it, and return it."""
        event = Event.now(type, run_id, job_id=job_id, **data)
        self.publish(event)
        return event


# The data keys worth surfacing in a one-line summary, first present wins:
# the "what happened" of the event (a state, some text, a tool, an outcome …).
SUMMARY_KEYS: tuple[str, ...] = (
    "state",
    "content",
    "text",
    "reply",
    "tool",
    "op",
    "line",
    "message",
    "summary",
    "outcome",
    "error",
    "url",
    "path",
)
SUMMARY_CLIP = 160


def _clip(value: Any, limit: int) -> str:
    flat = " ".join(str(value).split())
    if len(flat) <= limit:
        return flat
    keep = (limit - 1) // 2
    return f"{flat[:keep]}…{flat[-keep:]}"


def summarize_event(event: Event) -> dict[str, Any]:
    """The dense one-line reading of an event as structured fields — what
    ``sbxloop logs`` prints and what the daemon's log sink emits: task/agent
    ids, the first present :data:`SUMMARY_KEYS` value (as ``summary``, with
    the key it came from as ``summary_key``), the resource snapshot, tool
    args and any error — each clipped to a display line.

    Tool ``args`` for ``agent.tool_start``/``agent.tool_end`` are rendered
    with :func:`sbxloop.cli.cmdfmt.format_command`, so the boilerplate ``cd
    <run path> &&`` prefix collapses to ``cd $RUN &&`` and the command verb
    always survives. Only the returned summary changes: the stored event
    payload keeps the full, untouched command."""
    data = event.data
    out: dict[str, Any] = {}
    if data.get("task_id"):
        out["task"] = data["task_id"]
    if data.get("agent"):
        out["agent"] = data["agent"]
    picked = ""
    for key in SUMMARY_KEYS:
        if data.get(key):
            picked = key
            out["summary_key"] = key
            out["summary"] = str(data[key]).replace("\n", " ")[:SUMMARY_CLIP]
            break
    if data.get("disk_used_pct") is not None:
        out["disk"] = f"{data['disk_used_pct']}%"
        if data.get("mem_used_pct") is not None:
            out["mem"] = f"{data['mem_used_pct']}%"
        if data.get("load1") is not None:
            out["load"] = data["load1"]
        if data.get("level"):
            out["resource_level"] = data["level"]
    if data.get("args"):
        if event.type in (EventTypes.AGENT_TOOL_START, EventTypes.AGENT_TOOL_END):
            out["args"] = format_command(str(data["args"]))
        else:
            out["args"] = _clip(data["args"], 120)
    if picked != "error" and data.get("error"):
        out["error"] = _clip(data["error"], SUMMARY_CLIP)
    return out


class HostEventTypes:
    """Host-emitted event types (the worker's live in protocol.EventTypes)."""

    RUN_START = "run.start"
    RUN_STATE = "run.state"
    RUN_CONFIG_DRIFT = "run.config_drift"
    RUN_ARTIFACTS = "run.artifacts"
    # The pull request, opened or updated (`pr`, `url`, `branch`, `head_sha`,
    # `round`); `error` instead when delivery failed.
    RUN_DELIVER = "run.deliver"
    RUN_KEEP = "run.keep"
    RUN_END = "run.end"
    # The pipeline after the task graph. `run.state` already marks every
    # stage entry (the state *is* the stage); these carry what a stage
    # decided. `review.verdict`: our own critic's call on the PR (`round`,
    # `verdict`, `findings`, `blocking`, `url`, `summary`). `fix.round`: a
    # fix task appended (`round`, `kind`, `task_id`, `why`, `budget`).
    # `ci.status`: the folded check-run state, emitted on change only
    # (`round`, `state`, `total`, `pending`, `failed`, `head_sha`,
    # `waited_s`). `land.*`: the landing steps GitHub was asked for.
    # `run.merged` / `run.blocked`: how the pipeline ended.
    REVIEW_VERDICT = "review.verdict"
    # The fix round's answer spoken back onto the PR's own threads
    # (`round`, `addressed`, `refuted`, `unanswered`, `replied`, `resolved`,
    # `body_only`, `comment_url`).
    REVIEW_RECONCILED = "review.reconciled"
    FIX_ROUND = "fix.round"
    # A fix round's report left findings with no addressed/refuted/deferred
    # line (`round`, `task_id`, `anchors`); they ride into the next brief.
    FIX_UNANSWERED = "fix.unanswered"
    CI_STATUS = "ci.status"
    # The landing's judgment of the head's checks against the base (#611):
    # `required` (the gating names) and their `source`, `fix`,
    # `regressions`, `preexisting`, `advisory`, `ignored`, `baseline_sha`.
    LANDING_CHECKS = "landing.checks"
    LAND_UNDRAFT = "land.undraft"
    # A draft the loop did not make (#677): a person converted the PR back,
    # and the landing parks until they mark it ready (`pr`, `head`).
    LAND_HELD_BY_DRAFT = "land.held_by_draft"
    LAND_UPDATE = "land.update"
    # The base merges through a merge queue (#676): the PR entered it
    # (`pr`, `head`, `position`, `resumed` when it was already queued) …
    LAND_ENQUEUED = "land.enqueued"
    # … and the queue removed it unmerged (`pr`, `head`, `reason` as GitHub
    # words it, `failed` — the red checks on the queue's commit, which a
    # fix round gets; none means the run blocks).
    LAND_DEQUEUED = "land.dequeued"
    # Landing answered human threads nothing else in the pipeline would
    # ever speak to (`pr`, `acked`) — the reply is the not-silent part.
    LAND_HUMAN_ACK = "land.human_ack"
    # The ack pass hit ACK_CAP; the rest block truthfully (#613).
    LAND_HUMAN_ACK_CAPPED = "land.human_ack_capped"
    # An automated reviewer's changes-requested review stands past its one
    # fix round; the landing goes on and names it (#613).
    LAND_BOT_STANDING = "land.bot_standing"
    RUN_MERGED = "run.merged"
    # The opt-in merge gate parked the run: every bar cleared, the merge
    # awaiting one human approval (`pr`, `url`, `sha`, `review_rounds`,
    # `ci_rounds`).
    RUN_GATED = "run.gated"
    # The base requires an approving review the loop cannot give its own
    # PR (#675): every bar it can clear is cleared; the run parks and the
    # daemon waits for a person on GitHub (`pr`, `url`, `sha`,
    # `approvals_required`, `approvals_have`, `code_owners`).
    RUN_AWAITING_REVIEW = "run.awaiting_review"
    # Follow-up issues filed (or listed on the PR) after the merge (#517):
    # `pr`, `mode`, `filed` [{number, url, title}], `listed` (titles).
    RUN_FOLLOWUPS = "run.followups"
    RUN_BLOCKED = "run.blocked"
    # A non-terminal run closed out by daemon startup/staleness reconciliation.
    RUN_RECONCILED = "run.reconciled"
    # One authenticated request the service sandbox made on the agent's
    # behalf (#765): `credential`, `method`, `path`, `status` (or `error`),
    # `duration_s`, `phase`, `task_id`. Never the body, never a header.
    SERVICE_CALL = "service.call"
    # One dependency fetch the service sandbox ran for the agent (#766):
    # `ecosystem`, `verb`, `argv`, `exit_code` (or `error`), `duration_s`,
    # `phase`, `task_id`. The output tail rides `detail` only on failure.
    SANDBOX_FETCH = "sandbox.fetch"
    # What one workload task produced (#757), after each execute attempt:
    # `task_id`, `attempt`, `summary` (the report's result line), `files`
    # (how many data-directory files the task touched). The full output
    # lives on the task row, never on the wire.
    TASK_OUTPUT = "task.output"
    # The judge's verdict on one workload task (#756): `task_id`,
    # `attempt`, `passed`, `unmet`, `notes`.
    JUDGE_VERDICT = "judge.verdict"
    # The judge produced no usable verdict twice running (#756): `task_id`,
    # `attempt`, `error`. The run fails closed — a judge that cannot judge
    # never passes work.
    JUDGE_DEGRADED = "judge.degraded"
    # The task roster as the run will work it (also re-announced on resume,
    # with each task's persisted state).
    RUN_TASKS = "run.tasks"
    TASK_START = "task.start"
    TASK_STATE = "task.state"
    TASK_END = "task.end"
    # Also carries `status="advisory"` (the check failed under
    # `verify_mode = "advisory"` and blocked nothing) and `status="skipped"`
    # (`ci-only`: the pull request's checks are the verification) — #682.
    PHASE_END = "phase.end"
    # Once per run, before decomposition, when `verify_mode = "full"` and
    # the workspace shows a suite that needs services the sandbox does not
    # have (a compose file, testcontainers in a lockfile, `services:` in a
    # workflow): `evidence` names what was seen, `hint` the knob (#682).
    # A hint only — the mode never changes on its own.
    VERIFY_SERVICES_DETECTED = "verify.services_detected"
    POLICY_ALLOW = "policy.allow"
    POLICY_DENY = "policy.deny"
    SANDBOX_PROVISION_START = "sandbox.provision_start"
    # The language set this run provisions and where it came from
    # (config / detected from the workspace / the default) — #624.
    SANDBOX_LANGUAGES = "sandbox.languages"
    # One per versioned toolchain the run provisions (#627): the series
    # chosen and where it came from (a workspace declaration or the
    # registry default), so a probe failure can be read against the
    # interpreter the project asked for.
    SANDBOX_TOOLCHAIN = "sandbox.toolchain"
    # One per operator setup step (#681): the `apt_packages` install when a
    # template lacked any, and each `setup_commands` entry with its exit
    # code, duration and output tail (delivered secret values scrubbed).
    SANDBOX_SETUP = "sandbox.setup"
    SANDBOX_READY = "sandbox.ready"
    SANDBOX_PREBAKED = "sandbox.prebaked"
    SANDBOX_CLEANUP = "sandbox.cleanup"
    # Interactive chat: a user message entering the loop, the agent's reply,
    # and the resulting course change (when the reply's action was not
    # "continue").
    CHAT_MESSAGE = "chat.message"
    CHAT_REPLY = "chat.reply"
    CHAT_ACTION = "chat.action"
    # A gc sweep removed the run's on-disk payload (runs/<run>/); recorded on
    # the run so `logs` explains the missing directory and `resume` refuses.
    DAEMON_GC = "daemon.gc"


__all__ = [
    "SUMMARY_KEYS",
    "Event",
    "EventBus",
    "Hook",
    "HostEventTypes",
    "Subscriber",
    "summarize_event",
]
