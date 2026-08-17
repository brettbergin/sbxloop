"""The per-run event stream, mirrored into the daemon's log.

Every observable thing a run does is an :class:`~sbxloop.events.Event` on
the run's bus (see ``docs/architecture.md`` → Events). The daemon
persists them and, with Discord on, narrates them there — but without
this subscriber nothing between "claimed" and "settled" reaches the
journal at all. :func:`event_log_subscriber` is that subscriber: it logs
each event under the ``sbxloop.run`` logger at a level chosen by type, so
INFO reads as the run's lifecycle and DEBUG carries the firehose.

Tiers (:func:`level_for`):

- WARNING — the run degraded or something was refused: worker errors,
  tooling/resource warnings, permission denials, the tool-call cap,
  config drift.
- INFO — lifecycle: ``run.*``, task and phase start/state/end, sandbox
  provisioning, worker job start/end/result, GitHub op start/end,
  policy denials, chat (steering) traffic, gc.
- DEBUG — everything else: individual tool calls, agent messages and
  deltas, usage, heartbeats, stdout, resource samples, policy allows,
  op progress.
"""

from __future__ import annotations

import logging

from sbxloop.events import Event, HostEventTypes, summarize_event
from sbxloop.log import get_logger
from sbxloop_worker.protocol import EventTypes

RUN_LOGGER_NAME = "sbxloop.run"
run_log = get_logger(RUN_LOGGER_NAME)

WARNING_TYPES: frozenset[str] = frozenset(
    {
        EventTypes.WORKER_ERROR,
        EventTypes.SANDBOX_TOOLING_WARNING,
        EventTypes.SANDBOX_RESOURCES_WARNING,
        EventTypes.AGENT_PERMISSION_DENIED,
        EventTypes.AGENT_TOOL_CAP,
        HostEventTypes.RUN_CONFIG_DRIFT,
    }
)

INFO_TYPES: frozenset[str] = frozenset(
    {
        HostEventTypes.RUN_START,
        HostEventTypes.RUN_STATE,
        HostEventTypes.RUN_ARTIFACTS,
        HostEventTypes.RUN_DELIVER,
        HostEventTypes.RUN_REPORT,
        HostEventTypes.RUN_KEEP,
        HostEventTypes.RUN_END,
        HostEventTypes.TASK_START,
        HostEventTypes.TASK_STATE,
        HostEventTypes.TASK_END,
        HostEventTypes.PHASE_START,
        HostEventTypes.PHASE_END,
        HostEventTypes.POLICY_DENY,
        HostEventTypes.SANDBOX_PROVISION_START,
        HostEventTypes.SANDBOX_READY,
        HostEventTypes.SANDBOX_PREBAKED,
        HostEventTypes.SANDBOX_CLEANUP,
        HostEventTypes.CHAT_MESSAGE,
        HostEventTypes.CHAT_REPLY,
        HostEventTypes.CHAT_ACTION,
        HostEventTypes.DAEMON_GC,
        EventTypes.WORKER_START,
        EventTypes.WORKER_RESULT,
        EventTypes.WORKER_END,
        EventTypes.GH_OP_START,
        EventTypes.GH_OP_END,
    }
)


def level_for(event_type: str) -> int:
    """The log level an event of this type is mirrored at."""
    if event_type in WARNING_TYPES:
        return logging.WARNING
    if event_type in INFO_TYPES or event_type.startswith("run."):
        return logging.INFO
    return logging.DEBUG


def event_log_subscriber(event: Event) -> None:
    """Bus subscriber: log the event under ``sbxloop.run`` at
    :func:`level_for` its type, with the run/job ids and the same summary
    fields ``sbxloop logs`` prints."""
    run_log.log(
        level_for(event.type),
        event.type,
        run=event.run_id,
        job=event.job_id,
        **summarize_event(event),
    )


__all__ = [
    "INFO_TYPES",
    "RUN_LOGGER_NAME",
    "WARNING_TYPES",
    "event_log_subscriber",
    "level_for",
    "run_log",
]
