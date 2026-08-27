"""Concierge: the control channel's agent.

The daemon's Discord bridge relays chronology out and steering in; the
concierge is the agent people *talk to* in the control channel itself.
It knows how to operate sbxloop — every ``!sbx`` verb, through the same
:func:`sbxloop.daemon.control.dispatch` the commands use — how to queue new
work, and how to look up and explain runs, PRs and diffs.

It is an ordinary agent session in that it runs inside a sandbox (the
daemon's long-lived :class:`~sbxloop.daemon.agentbox.DaemonAgent` box) and
never touches the host directly: everything it can *do* is a **host tool**
(``JobRequest.host_tools``), executed here on the daemon host and answered
through the worker protocol's response files. The SDK session is resumed
message after message (``resume_session_id`` persisted in ``daemon_state``),
so the conversation has memory; after ``[concierge] session_turns`` a
fresh session starts so context does not grow forever.

Transport-agnostic: no Discord import. The bridge calls
:meth:`Concierge.submit_turn` and renders the reply; other channels could
do the same.

Threading: turns run one at a time on the concierge's own worker thread
(``submit_turn`` returns a Future; ``pending`` says how many are queued
behind the running one). Tool handlers run on the WorkerClient's host-tool
pool while the session is blocked; they are serialised by
``_tool_lock`` — the daemon loop, the daemon store and the inbox have
their own locks, and the concierge keeps a private read-only
:class:`~sbxloop.engine.store.StateStore` connection so it never shares the
engine thread's.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol
from urllib.parse import quote

from sbxloop.cli.tui import format_event
from sbxloop.config import Config
from sbxloop.daemon.control import dispatch, plain
from sbxloop.daemon.loop import day_window
from sbxloop.daemon.store import DaemonStore
from sbxloop.daemon.versions import VersionProbe
from sbxloop.engine.prompts import bullet_list, render
from sbxloop.engine.store import StateStore
from sbxloop.errors import (
    DaemonError,
    GithubOpsError,
    ProvisionError,
    SbxError,
    SbxloopError,
    WorkerError,
    WorkerTimeoutError,
)
from sbxloop.events import EventBus
from sbxloop.ids import new_job_id
from sbxloop.log import get_logger, log_buffer
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import (
    EventTypes,
    HostToolCall,
    HostToolResponse,
    HostToolSpec,
    JobRequest,
    Usage,
)

if TYPE_CHECKING:
    from sbxloop.daemon.github import DaemonGithub
    from sbxloop.daemon.loop import DaemonLoop
    from sbxloop.daemon.model import WorkItem
    from sbxloop.daemon.sources import InboxSource
    from sbxloop.gh.ops import GithubOps

log = get_logger(__name__)

# Persona stamped on the concierge's agent.* events (transcript attribution).
CONCIERGE_AGENT = "concierge"
# The events of a concierge turn carry this run id.
CONCIERGE_RUN_ID = "concierge"
# daemon_state keys
STATE_SESSION_ID = "concierge_session_id"
STATE_SESSION_TURNS = "concierge_session_turns"

_RUN_STATES = ["pending", "running", "completed", "failed", "cancelled"]
#: Run states that mean the run is over — nothing more will happen to it, so a
#: watch on one of these is answered immediately instead of registered.
_FINISHED_RUN_STATES = frozenset({"completed", "failed", "cancelled"})
#: Levels ``daemon_log`` accepts, coarsest last — the filter is at-or-above.
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# GitHub's ``state_reason`` for a close. ``completed`` means the thing was
# actually done; ``not_planned`` is the triage verdict — duplicate, won't fix,
# stale. Nothing else is accepted, so the model cannot invent a reason.
CLOSE_REASONS = ("completed", "not_planned")
#: Appended to a tool call's ``by`` for GitHub-facing attribution (see
#: ``_tool_handler``). A transport's ``on_watch`` callback receives this same
#: tagged string as its ``requester`` and must strip it back off before
#: looking a requester up by the untagged form it was remembered under —
#: see ``DiscordBridge.on_watch``.
VIA_CONCIERGE_SUFFIX = " (via concierge)"


class ConciergeReply(NamedTuple):
    text: str
    ok: bool = True
    error: str | None = None


class SessionHost(Protocol):
    """Where the concierge's session runs (``DaemonAgent`` in production)."""

    def client(self) -> WorkerClient: ...

    def note_failure(self, exc: BaseException) -> bool: ...

    def close(self) -> None: ...


ToolCallback = Callable[[str, dict[str, Any], HostToolResponse], None]
ToolImpl = Callable[[dict[str, Any], str], str]


class HostTool(NamedTuple):
    spec: HostToolSpec
    impl: ToolImpl


class Concierge:
    def __init__(
        self,
        config: Config,
        *,
        loop: DaemonLoop,
        dstore: DaemonStore,
        store_factory: Callable[[], StateStore],
        inbox: InboxSource | None,
        github: DaemonGithub | None,
        host: SessionHost,
        bus: EventBus,
        clock: Callable[[], float] = time.time,
        versions: VersionProbe | None = None,
        on_watch: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self.config = config
        self.loop = loop
        self.dstore = dstore
        self._store_factory = store_factory
        self._store: StateStore | None = None
        self.inbox = inbox
        self.github = github if config.concierge.github_tools else None
        self.host = host
        self.bus = bus
        self.clock = clock
        # The daemon builds one probe and shares it, so the startup drift
        # check warms the PyPI memo for the first "are we up to date?".
        # Injected whole in tests, so no unit test reaches PyPI or runs `sbx`.
        self.versions = versions if versions is not None else VersionProbe()
        # Transport seam: the bridge hands in a callback that records who wants
        # a ping when a run finishes. None means this transport cannot notify.
        self._on_watch = on_watch
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sbxloop-concierge")
        self._pending = 0
        self._state_lock = threading.Lock()
        self._tool_lock = threading.Lock()
        self._closed = False
        self._tools: dict[str, HostTool] = {t.spec.name: t for t in self._build_tools()}
        self._warm: threading.Thread | None = None

    # -- public --------------------------------------------------------------

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools)

    @property
    def pending(self) -> int:
        with self._state_lock:
            return self._pending

    def warm_up(self) -> None:
        """Provision the session sandbox in the background so the first
        mention does not pay the microVM boot; failures are logged only —
        the first turn reports them properly."""

        def run() -> None:
            try:
                self.host.client()
            except SbxloopError as exc:
                log.warning("concierge.warm_up_failed", error=str(exc)[:300])

        self._warm = threading.Thread(target=run, name="sbxloop-concierge-warmup", daemon=True)
        self._warm.start()

    def submit_turn(
        self,
        text: str,
        *,
        author: str,
        on_tool: ToolCallback | None = None,
    ) -> Future[ConciergeReply]:
        """Queue one message; the Future resolves with the reply."""
        with self._state_lock:
            if self._closed:
                raise RuntimeError("concierge is closed")
            self._pending += 1

        def run() -> ConciergeReply:
            with self._state_lock:
                self._pending -= 1
            return self._run_turn(text, author=author, on_tool=on_tool)

        return self._executor.submit(run)

    def reset_session(self) -> None:
        self.dstore.set_value(STATE_SESSION_ID, None)
        self.dstore.set_value(STATE_SESSION_TURNS, None)
        log.info("concierge.session_reset")

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.host.close()
        store, self._store = self._store, None
        if store is not None:
            store.close()

    # -- one turn ---------------------------------------------------------------

    def _run_turn(self, text: str, *, author: str, on_tool: ToolCallback | None) -> ConciergeReply:
        session_id, turns = self._session()
        if turns >= self.config.concierge.session_turns:
            log.info("concierge.session_rotated", turns=turns)
            session_id, turns = None, 0
        started = time.monotonic()
        try:
            reply = self._attempt(text, author=author, session_id=session_id, on_tool=on_tool)
        except SbxloopError as exc:
            retry = False
            if session_id is not None and _looks_like_lost_session(exc):
                # The sandbox forgot the session (rebuilt VM, expired
                # store): start over rather than fail every message.
                log.warning("concierge.session_lost", error=str(exc)[:200])
                self.reset_session()
                session_id, turns, retry = None, 0, True
            elif isinstance(exc, WorkerError | SbxError) and not isinstance(
                exc, WorkerTimeoutError
            ):
                # A dead sandbox costs one hiccup: DaemonAgent drops it
                # (rate-limited) and the retry re-provisions — with a fresh
                # session store, so the resume id is gone too.
                if self.host.note_failure(exc):
                    self.reset_session()
                    session_id, turns, retry = None, 0, True
            if not retry:
                return self._error_reply(exc, started)
            try:
                reply = self._attempt(text, author=author, session_id=None, on_tool=on_tool)
            except SbxloopError as exc2:
                return self._error_reply(exc2, started)
        new_session, output = reply
        if new_session:
            self.dstore.set_value(STATE_SESSION_ID, new_session)
            self.dstore.set_value(STATE_SESSION_TURNS, str(turns + 1))
        log.info(
            "concierge.turn",
            by=author,
            chars=len(output),
            session=new_session,
            duration_s=round(time.monotonic() - started, 1),
        )
        return ConciergeReply(_clip(output, self.config.concierge.max_reply_chars))

    def _attempt(
        self,
        text: str,
        *,
        author: str,
        session_id: str | None,
        on_tool: ToolCallback | None,
    ) -> tuple[str | None, str]:
        cfg = self.config.concierge
        job = JobRequest(
            job_id=new_job_id(),
            run_id=CONCIERGE_RUN_ID,
            kind="agent.session",
            prompt=self._preamble(author) + "\n---\n" + text,
            system_message=self._system_message(),
            model=cfg.model or self.config.model,
            resume_session_id=session_id,
            # Nothing to edit in the scratch sandbox: read-only, and no SDK
            # built-ins at all — every capability is a host tool.
            permission_mode="read_only",
            available_tools=[],
            expect="text",
            timeout_s=cfg.timeout_s,
            max_tool_calls=cfg.max_tool_calls,
            host_tools=[t.spec for t in self._tools.values()],
            host_tool_timeout_s=min(cfg.timeout_s, 120.0),
        )

        def handler(call: HostToolCall) -> HostToolResponse:
            response = self._tool_handler(call, author=author)
            if on_tool is not None:
                try:
                    on_tool(call.name, call.arguments, response)
                except Exception:
                    log.debug("concierge.on_tool_failed", exc_info=True)
            return response

        client = self.host.client()
        result = client.submit(job, agent=CONCIERGE_AGENT, tool_handler=handler)
        if result.status != "ok":
            message = result.error.message if result.error is not None else result.status
            if result.status == "timeout":
                raise WorkerTimeoutError(message)
            raise WorkerError(message)
        return result.session_id, (result.output_text or "").strip()

    def _error_reply(self, exc: BaseException, started: float) -> ConciergeReply:
        if isinstance(exc, WorkerTimeoutError) or "timed out" in str(exc).lower():
            error = (
                f"that took longer than {self.config.concierge.timeout_s:.0f}s — "
                "try a narrower question"
            )
        elif isinstance(exc, ProvisionError | DaemonError) and "COPILOT_GITHUB_TOKEN" in str(exc):
            error = "the concierge needs COPILOT_GITHUB_TOKEN on the daemon host"
        else:
            error = _one_line(str(exc), 300)
        log.warning(
            "concierge.turn_failed",
            error=str(exc)[:300],
            duration_s=round(time.monotonic() - started, 1),
        )
        return ConciergeReply("", ok=False, error=error)

    def _session(self) -> tuple[str | None, int]:
        session_id = self.dstore.get_value(STATE_SESSION_ID)
        raw = self.dstore.get_value(STATE_SESSION_TURNS)
        try:
            turns = int(raw) if raw else 0
        except ValueError:
            turns = 0
        return session_id, turns

    # -- prompt --------------------------------------------------------------

    def _system_message(self) -> str:
        daemon = self.config.daemon
        notes = [
            f"poll interval {daemon.poll_interval_s:g}s; at most {daemon.max_runs_per_day} "
            f"runs per calendar day in {daemon.run_cap_timezone} (the count resets at "
            f"00:00 {daemon.run_cap_timezone}); "
            "a consecutive-failure breaker pauses dispatch",
            f"backlog mode: {daemon.backlog}",
            "Discord: one thread per run"
            if self.config.discord.thread_per_run
            else "Discord: runs post in the control channel",
        ]
        return render(
            "concierge",
            command_prefix=self.config.discord.command_prefix,
            repo=self.config.github.repo or "(no GitHub repository configured)",
            inbox_dir=self.config.daemon.inbox_dir or "(no inbox configured)",
            model=self.config.concierge.model or self.config.model,
            backlog_label=self.config.daemon.backlog_label,
            trigger_label=self.config.daemon.trigger_label,
            tool_notes=bullet_list(
                [f"`{t.spec.name}` — {t.spec.description}" for t in self._tools.values()]
            ),
            daemon_notes=bullet_list(notes),
        )

    def _preamble(self, author: str) -> str:
        try:
            status = self.loop.status()
        except Exception as exc:
            # The loop is another thread's object; never fail a turn on it.
            reason = _one_line(str(exc), 80)
            return f"[situation] daemon status unavailable ({reason}) | speaker: {author}"
        cur = status.get("current")
        current = f"{cur['run_id']} — {_one_line(str(cur.get('title', '')), 80)}" if cur else "idle"
        stamp = time.strftime("%H:%M", time.localtime(self.clock()))
        return (
            f"[situation @ {stamp}] current: {current} | queued: {status.get('queued', 0)} | "
            f"paused: {status.get('paused', False)} | breaker: "
            f"{'open' if status.get('breaker_open') else 'closed'} | runs today "
            f"({status.get('run_cap_timezone', 'UTC')}): "
            f"{status.get('runs_today', 0)}/{status.get('max_runs_per_day', '?')}, resets "
            f"00:00 {status.get('run_cap_timezone', 'UTC')} | "
            f"speaker: {author}"
        )

    # -- tools ---------------------------------------------------------------

    def _tool_handler(self, call: HostToolCall, *, author: str) -> HostToolResponse:
        tool = self._tools.get(call.name)
        if tool is None:
            return HostToolResponse(
                call_id=call.call_id, ok=False, error=f"unknown tool {call.name!r}"
            )
        by = f"{author}{VIA_CONCIERGE_SUFFIX}"
        started = time.monotonic()
        try:
            with self._tool_lock:
                text = tool.impl(dict(call.arguments), by)
        except Exception as exc:
            log.warning(
                "concierge.tool_failed",
                tool=call.name,
                by=author,
                error=f"{type(exc).__name__}: {exc}"[:300],
                exc_info=True,
            )
            return HostToolResponse(
                call_id=call.call_id,
                ok=False,
                text=f"tool {call.name} failed: {type(exc).__name__}: {exc}",
                error=f"{type(exc).__name__}: {exc}",
            )
        log.info(
            "concierge.tool",
            tool=call.name,
            by=author,
            args=_one_line(json.dumps(call.arguments, default=str), 200),
            duration_s=round(time.monotonic() - started, 2),
        )
        return HostToolResponse(
            call_id=call.call_id,
            ok=True,
            text=_clip(text, self.config.concierge.max_tool_result_chars),
        )

    def _build_tools(self) -> list[HostTool]:
        prefix = self.config.discord.command_prefix
        tools = [
            HostTool(
                HostToolSpec(
                    name="sbx_control",
                    description=(
                        f"Run one operator command exactly as `{prefix} <command>` would: "
                        "status | pause | resume | cancel [--retry] | queue | items | "
                        "abandon <item> [reason] | retry <item> | requeue <item>. Pass the "
                        "command line without the prefix. Mutating commands take effect "
                        "immediately."
                    ),
                    parameters=_schema({"command": {"type": "string"}}, ["command"]),
                ),
                self._tool_sbx_control,
            ),
            HostTool(
                HostToolSpec(
                    name="list_runs",
                    description="Recent runs, newest first: run id, state, age, work item, title.",
                    parameters=_schema(
                        {
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                            "state": {"type": "string", "enum": _RUN_STATES},
                        }
                    ),
                ),
                self._tool_list_runs,
            ),
            HostTool(
                HostToolSpec(
                    name="run_detail",
                    description=(
                        "Everything known about one run: outcome, state, tasks, tracking "
                        "issue / PR / delivery error, standing guidance, its work item and "
                        "the Discord thread where it can be steered."
                    ),
                    parameters=_schema({"run_id": {"type": "string"}}, ["run_id"]),
                ),
                self._tool_run_detail,
            ),
            HostTool(
                HostToolSpec(
                    name="watch_run",
                    description=(
                        "Register interest in a run (or a work item, by its id) so the person "
                        "who asked is @mentioned in the control channel with the outcome when "
                        "it finishes. If the run has already finished, answers with the "
                        "outcome immediately instead of registering. Watches are persisted, so "
                        "they survive a daemon restart."
                    ),
                    parameters=_schema(
                        {"run_id_or_item_id": {"type": "string"}}, ["run_id_or_item_id"]
                    ),
                ),
                self._tool_watch_run,
            ),
            HostTool(
                HostToolSpec(
                    name="run_events",
                    description=(
                        "The last N events of a run's chronology, one line each (agent "
                        "messages, tool calls, phase/task/run lifecycle). Filter with a "
                        "type prefix such as 'agent.message', 'task.', 'run.', 'chat.'."
                    ),
                    parameters=_schema(
                        {
                            "run_id": {"type": "string"},
                            "type_prefix": {"type": "string"},
                            "tail": {"type": "integer", "minimum": 1, "maximum": 200},
                        },
                        ["run_id"],
                    ),
                ),
                self._tool_run_events,
            ),
            HostTool(
                HostToolSpec(
                    name="item_detail",
                    description=(
                        "One work item (ids look like gh:12 or inbox:name.md): source, kind, "
                        "state, attempts, last error, its runs and the latest run's thread."
                    ),
                    parameters=_schema({"item_id": {"type": "string"}}, ["item_id"]),
                ),
                self._tool_item_detail,
            ),
            HostTool(
                HostToolSpec(
                    name="enqueue_work",
                    description=(
                        "Queue NEW work for the daemon as an inbox item. Write a "
                        "self-contained title and body (what to build/change, acceptance "
                        "criteria, constraints) — the run's agents see only this text. The "
                        "daemon picks it up on its next poll."
                    ),
                    parameters=_schema(
                        {
                            "title": {"type": "string", "maxLength": 120},
                            "body": {"type": "string"},
                        },
                        ["title", "body"],
                    ),
                ),
                self._tool_enqueue_work,
            ),
            HostTool(
                HostToolSpec(
                    name="version_status",
                    description=(
                        "Is this daemon running current code? Reports the installed sbxloop, "
                        "sbxloop-worker and sbx versions, the latest sbxloop/sbxloop-worker "
                        "releases on PyPI, and whether the host is behind. Every merge to the "
                        "project's main branch publishes a patch, while deploying to this host "
                        "is manual, so drift is normal and worth checking. You cannot upgrade "
                        "anything — that is a human step on the daemon host — so report what "
                        "you find and say who has to act."
                    ),
                    parameters=_schema({}),
                ),
                self._tool_version_status,
            ),
            HostTool(
                HostToolSpec(
                    name="run_usage",
                    description=(
                        "What one run spent: input/output tokens per agent persona and "
                        "totalled, with the model(s) used. Copilot spend is otherwise "
                        "invisible from chat. Runs from before usage reporting — or a "
                        "backend that does not report it — say so rather than showing zero."
                    ),
                    parameters=_schema({"run_id": {"type": "string"}}, ["run_id"]),
                ),
                self._tool_run_usage,
            ),
            HostTool(
                HostToolSpec(
                    name="usage_today",
                    description=(
                        "Token spend across the last 24 hours, per run and totalled, next "
                        "to the daily run cap. Use it for 'how much have we spent today?'. "
                        "Tokens are attributed to when they were spent, so a run that "
                        "started yesterday and continued today counts only today's half."
                    ),
                    parameters=_schema({}),
                ),
                self._tool_usage_today,
            ),
            HostTool(
                HostToolSpec(
                    name="daemon_log",
                    description=(
                        "The daemon's own recent log lines, from its in-process ring "
                        "buffer — the journal, without ssh. Use it to answer 'what is the "
                        "daemon doing?' or 'why is nothing running?' by quoting the "
                        "`daemon.idle`, `breaker` and `github.poll_failed` lines. "
                        "tail: how many records (default 50, max 500). level: keep only "
                        "records at or above DEBUG/INFO/WARNING/ERROR. grep: a plain "
                        "case-insensitive substring, NOT a regular expression."
                    ),
                    parameters=_schema(
                        {
                            "tail": {"type": "integer", "minimum": 1, "maximum": 500},
                            "level": {"type": "string", "enum": list(_LOG_LEVELS)},
                            "grep": {"type": "string"},
                        }
                    ),
                ),
                self._tool_daemon_log,
            ),
        ]
        if self.github is not None and self.config.github.repo:
            tools.append(
                HostTool(
                    HostToolSpec(
                        name="github_get",
                        description=(
                            f"Read from GitHub repository {self.config.github.repo}: "
                            "what=pr (summary), pr_files (changed files), pr_diff (patches), "
                            "issue, issue_comments — with number; what=file with path "
                            "(and optional ref)."
                        ),
                        parameters=_schema(
                            {
                                "what": {
                                    "type": "string",
                                    "enum": [
                                        "pr",
                                        "pr_files",
                                        "pr_diff",
                                        "issue",
                                        "issue_comments",
                                        "file",
                                    ],
                                },
                                "number": {"type": "integer", "minimum": 1},
                                "path": {"type": "string"},
                                "ref": {"type": "string"},
                            },
                            ["what"],
                        ),
                    ),
                    self._tool_github_get,
                )
            )
            tools.append(
                HostTool(
                    HostToolSpec(
                        name="pr_status",
                        description=(
                            "Report how a pull request in "
                            f"{self.config.github.repo} is doing: CI check runs "
                            "(with the failing ones' URLs), review state and reviewers, "
                            "mergeability, and whether the branch is behind its base. "
                            "Read-only: it never merges, closes or writes anything."
                        ),
                        parameters=_schema(
                            {"number": {"type": "integer", "minimum": 1}},
                            ["number"],
                        ),
                    ),
                    self._tool_pr_status,
                )
            )
        if (
            self.github is not None
            and self.config.github.repo
            and self.config.concierge.create_issues
        ):
            backlog = self.config.daemon.backlog_label
            trigger = self.config.daemon.trigger_label
            tools.append(
                HostTool(
                    HostToolSpec(
                        name="create_issue",
                        description=(
                            f"File a NEW issue in {self.config.github.repo} describing a "
                            "feature or bug the person asked for. Write a clear title and a "
                            f"self-contained body. The issue gets the `{backlog}` label "
                            f"(triage); it does NOT run until the `{trigger}` label is added "
                            "— after creating it, ask the person whether to add that label."
                        ),
                        parameters=_schema(
                            {
                                "title": {"type": "string", "maxLength": 200},
                                "body": {"type": "string"},
                            },
                            ["title", "body"],
                        ),
                    ),
                    self._tool_create_issue,
                )
            )
            tools.append(
                HostTool(
                    HostToolSpec(
                        name="list_issues",
                        description=(
                            f"Open issues in {self.config.github.repo}: by default the ones "
                            f"carrying the `{backlog}` label (the triage backlog — work that "
                            "is waiting for someone to say run it); all=true lists every open "
                            "issue; label narrows to one label. Each line: number, title, "
                            "labels, age, author, comments, url. After listing the backlog, "
                            "ask the person whether any of them should be worked."
                        ),
                        parameters=_schema(
                            {
                                "all": {"type": "boolean"},
                                "label": {"type": "string"},
                                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                            }
                        ),
                    ),
                    self._tool_list_issues,
                )
            )
            tools.append(
                HostTool(
                    HostToolSpec(
                        name="label_issue_for_run",
                        description=(
                            f"Add the `{trigger}` label to an issue so the daemon claims and "
                            "runs it on its next poll. Only after the person explicitly said "
                            "yes to running it."
                        ),
                        parameters=_schema(
                            {"number": {"type": "integer", "minimum": 1}}, ["number"]
                        ),
                    ),
                    self._tool_label_issue_for_run,
                )
            )
            tools.append(
                HostTool(
                    HostToolSpec(
                        name="comment_on_issue",
                        description=(
                            f"Post a comment on an issue in {self.config.github.repo}: a "
                            "reply to whoever filed it, a triage note, a pointer to the "
                            "issue it duplicates. Write it as it should read on GitHub — "
                            "the author gets a notification and sees only the comment, not "
                            "this conversation. It is attributed to the person who asked "
                            "you. Labels and open/closed state are left untouched."
                        ),
                        parameters=_schema(
                            {
                                "number": {"type": "integer", "minimum": 1},
                                "body": {"type": "string"},
                            },
                            ["number", "body"],
                        ),
                    ),
                    self._tool_comment_on_issue,
                )
            )
            tools.append(
                HostTool(
                    HostToolSpec(
                        name="close_issue",
                        description=(
                            f"Close an issue in {self.config.github.repo}. reason="
                            "`completed` when the work is genuinely done, `not_planned` for "
                            "a duplicate, a won't-fix or something stale. Pass `comment` to "
                            "say why — that comment is the whole explanation the person who "
                            "filed it ever sees, so name the duplicate or the reason there. "
                            "ONLY after they explicitly said yes to closing THAT number: "
                            "quote their words in `confirmation`. Never close on your own "
                            "initiative and never merely to tidy the backlog."
                        ),
                        parameters=_schema(
                            {
                                "number": {"type": "integer", "minimum": 1},
                                "reason": {"type": "string", "enum": list(CLOSE_REASONS)},
                                "comment": {"type": "string"},
                                "confirmation": {"type": "string"},
                            },
                            ["number", "reason", "confirmation"],
                        ),
                    ),
                    self._tool_close_issue,
                )
            )
        return tools

    # tool implementations — each returns text for the model

    def _tool_sbx_control(self, args: dict[str, Any], by: str) -> str:
        command = str(args.get("command", "")).strip()
        if not command:
            return "usage: sbx_control(command) — e.g. status, pause, cancel --retry, queue"
        reply = dispatch(
            self.loop, command, prefix=self.config.discord.command_prefix, by=by, via="concierge"
        )
        text = plain(reply.text)
        if reply.status is not None:
            text += "\n" + _status_detail(reply.status)
        if not reply.ok:
            text = f"(command not accepted) {text}"
        return text

    def _tool_list_runs(self, args: dict[str, Any], by: str) -> str:
        limit = _int_arg(args, "limit", 10, 1, 50)
        state = args.get("state")
        runs = sorted(self.store.list_runs(), key=lambda r: r.created_at, reverse=True)
        if state:
            runs = [r for r in runs if r.state == state]
        if not runs:
            return "no runs recorded" + (f" in state {state}" if state else "")
        now = self.clock()
        lines = []
        for run in runs[:limit]:
            item_id = self.dstore.item_for_run(run.run_id)
            item = self.dstore.get(item_id) if item_id else None
            title = _one_line(item.title if item else run.outcome, 80)
            lines.append(
                f"{run.run_id} · {run.state} · {_age(now - run.updated_at)} ago · "
                f"{item_id or '(cli)'} · {title}"
            )
        more = len(runs) - limit
        return "\n".join(lines) + (f"\n… {more} more" if more > 0 else "")

    def _tool_run_detail(self, args: dict[str, Any], by: str) -> str:
        run_id = str(args.get("run_id", "")).strip()
        if not run_id:
            return "run_id is required"
        try:
            run = self.store.get_run(run_id)
        except SbxloopError:
            return f"no run {run_id!r} in this daemon's state store"
        tasks = self.store.get_tasks(run_id)
        report = self.loop.report_for(run_id)
        item_id = self.dstore.item_for_run(run_id)
        item = self.dstore.get(item_id) if item_id else None
        thread = self.dstore.discord_thread(run_id)
        lines = [
            f"run {run.run_id}: state={run.state}, created {_age(self.clock() - run.created_at)} "
            f"ago, updated {_age(self.clock() - run.updated_at)} ago",
            f"outcome: {_one_line(run.outcome, 400)}",
            f"tasks: {report.task_summary}",
        ]
        for task in tasks:
            lines.append(
                f"  - {task.spec.id} [{task.state}] {_one_line(task.spec.title, 100)}"
                f" (revisions {task.revisions}, replans {task.replans})"
            )
        if report.tracking_issue:
            lines.append(f"tracking issue: #{report.tracking_issue[0]} {report.tracking_issue[1]}")
        if report.delivery:
            lines.append(f"delivered PR: #{report.delivery[0]} {report.delivery[1]}")
        if report.delivery_error:
            lines.append(f"delivery error: {_one_line(report.delivery_error, 300)}")
        if report.filed:
            lines.append(f"filed backlog: {', '.join(report.filed)}")
        guidance = self.store.get_run_guidance(run_id)
        if guidance:
            lines.append("standing guidance:")
            lines.extend(f"  - {_one_line(g, 200)}" for g in guidance)
        if item is not None:
            lines.append(
                f"work item: {item.item_id} [{item.state}] {item.kind} · attempts {item.attempts}"
                + (f" · last error: {_one_line(item.last_error, 200)}" if item.last_error else "")
                + (f" · {item.url}" if item.url else "")
            )
        live = self.loop.current
        if live is not None and live.run_id == run_id:
            lines.append("this run is LIVE right now")
        if thread is not None:
            lines.append(
                f"Discord thread: <#{thread.thread_id}> — steering messages go there, "
                "not in the control channel"
            )
        return "\n".join(lines)

    def _tool_watch_run(self, args: dict[str, Any], by: str) -> str:
        ident = str(args.get("run_id_or_item_id", "")).strip()
        if not ident:
            return "run_id_or_item_id is required"
        run = None
        try:
            run = self.store.get_run(ident)
        except SbxloopError:
            run = None
        if run is None:
            item = self.dstore.get(ident)
            if item is None:
                return f"no run or work item {ident!r} in this daemon's state store"
            runs = self.dstore.runs_for_item(ident)
            if not runs:
                return (
                    f"work item {ident} has no runs yet — it has not started, "
                    "so there is nothing to watch"
                )
            try:
                run = self.store.get_run(runs[-1])
            except SbxloopError:
                return f"no run {runs[-1]!r} in this daemon's state store"
        run_id = run.run_id
        if run.state in _FINISHED_RUN_STATES:
            report = self.loop.report_for(run_id)
            lines = [
                f"run {run_id} already finished: state={run.state}",
                f"outcome: {_one_line(run.outcome, 400)}",
                f"tasks: {report.task_summary}",
            ]
            if report.tracking_issue:
                lines.append(
                    f"tracking issue: #{report.tracking_issue[0]} {report.tracking_issue[1]}"
                )
            if report.delivery:
                lines.append(f"delivered PR: #{report.delivery[0]} {report.delivery[1]}")
            if report.delivery_error:
                lines.append(f"delivery error: {_one_line(report.delivery_error, 300)}")
            if report.filed:
                lines.append(f"filed backlog: {', '.join(report.filed)}")
            return "\n".join(lines)
        if self._on_watch is None:
            return (
                "watching is not available on this transport — no notifier is wired, "
                f"so I cannot ping you when {run_id} finishes"
            )
        note = self._on_watch(run_id, by)
        confirm = (
            f"watching {run_id} (state {run.state}) — I'll ping {by} in the control "
            "channel with the outcome when it finishes. The watch is persisted, so it "
            "survives a daemon restart."
        )
        return f"{confirm}\n{note}" if note else confirm

    def _tool_run_events(self, args: dict[str, Any], by: str) -> str:
        run_id = str(args.get("run_id", "")).strip()
        if not run_id:
            return "run_id is required"
        prefix = args.get("type_prefix") or None
        tail = _int_arg(args, "tail", 40, 1, 200)
        try:
            events = [event for _seq, event in self.store.events(run_id, type_prefix=prefix)]
        except SbxloopError as exc:
            return f"cannot read events for {run_id}: {_one_line(str(exc), 200)}"
        if not events:
            return f"no events for {run_id}" + (f" with prefix {prefix!r}" if prefix else "")
        shown = events[-tail:]
        lines = [format_event(e) for e in shown]
        head = f"({len(events)} events; showing last {len(shown)})\n" if len(events) > tail else ""
        return head + "\n".join(lines)

    def _tool_item_detail(self, args: dict[str, Any], by: str) -> str:
        item_id = str(args.get("item_id", "")).strip()
        item = self.dstore.get(item_id) if item_id else None
        if item is None:
            return f"no work item {item_id!r} (ids look like gh:12 or inbox:name.md)"
        runs = self.dstore.runs_for_item(item_id)
        lines = [
            f"{item.item_id}: {item.kind} from {item.source} · state {item.state} · "
            f"attempts {item.attempts}",
            f"title: {_one_line(item.title, 200)}",
        ]
        if item.url:
            lines.append(f"url: {item.url}")
        if item.last_error:
            lines.append(f"last error: {_one_line(item.last_error, 300)}")
        if item.body:
            lines.append(f"body: {_one_line(item.body, 600)}")
        lines.append(f"runs: {', '.join(runs) if runs else '(none yet)'}")
        if runs:
            thread = self.dstore.discord_thread(runs[-1])
            if thread is not None:
                lines.append(f"latest run's Discord thread: <#{thread.thread_id}>")
        return "\n".join(lines)

    def _tool_enqueue_work(self, args: dict[str, Any], by: str) -> str:
        if self.inbox is None:
            return (
                "cannot enqueue: this daemon has no inbox source (set [daemon] inbox_dir or "
                "start it with --inbox)"
            )
        title = _one_line(str(args.get("title", "")).strip(), 120)
        body = str(args.get("body", "")).strip()
        if not title or not body:
            return "both title and body are required"
        item_id = self.inbox.enqueue(title, body, by=by)
        status = self.loop.status()
        note = ""
        if status.get("paused"):
            note = " The daemon is PAUSED — nothing runs until `resume`."
        elif status.get("breaker_open"):
            note = " The breaker is OPEN — nothing runs until it resets."
        return (
            f"queued {item_id} — the daemon picks it up on its next poll "
            f"(every {self.config.daemon.poll_interval_s:g}s) and runs it after anything "
            f"already queued.{note}"
        )

    def _tool_version_status(self, args: dict[str, Any], by: str) -> str:
        """Installed versus latest. The PyPI half is best effort by
        construction (see :mod:`sbxloop.daemon.versions`), so this returns a
        report rather than failing when the network is unavailable."""
        try:
            return self.versions.summary()
        except (WorkerError, SbxError, DaemonError) as exc:
            return f"reading versions failed: {_one_line(str(exc), 300)}"

    def _usage_for_run(self, run_id: str, *, since: float = 0.0) -> _RunUsage:
        """Fold a run's ``agent.usage`` events into a total and a per-persona
        breakdown. ``since`` drops samples older than an epoch stamp, which is
        how ``usage_today`` attributes tokens to the day they were spent
        rather than to the day the run started."""
        total = Usage()
        by_agent: dict[str, Usage] = {}
        models: list[str] = []
        samples = 0
        # One `agent.usage` event is one assistant turn, and turns — not
        # jobs — are what a run is billed and timed by: every turn re-sends
        # the whole session context. Counting them per persona is how "where
        # did it go?" gets an actionable answer instead of a token total.
        turns: dict[str, int] = {}
        jobs: dict[str, set[str]] = {}
        for _seq, event in self.store.events(run_id, type_prefix=EventTypes.AGENT_USAGE):
            if event.ts < since:
                continue
            sample = _usage_from_event(event.data)
            total = total.merged(sample)
            who = str(event.data.get("agent") or "unknown")
            by_agent[who] = by_agent.get(who, Usage()).merged(sample)
            turns[who] = turns.get(who, 0) + 1
            if event.job_id:
                jobs.setdefault(who, set()).add(event.job_id)
            if sample.model and sample.model not in models:
                models.append(sample.model)
            samples += 1
        return _RunUsage(
            total, by_agent, models, samples, turns, {k: len(v) for k, v in jobs.items()}
        )

    def _tool_run_usage(self, args: dict[str, Any], by: str) -> str:
        run_id = str(args.get("run_id", "")).strip()
        if not run_id:
            return "run_id is required"
        try:
            self.store.get_run(run_id)
        except SbxloopError:
            return f"no run {run_id!r} in this daemon's state store"
        try:
            usage = self._usage_for_run(run_id)
        except SbxloopError as exc:
            return f"cannot read usage for {run_id}: {_one_line(str(exc), 200)}"
        if not usage.recorded:
            return (
                f"no usage recorded for {run_id}. Either the run predates usage reporting "
                "or its backend does not report it — this is not the same as zero spend."
            )
        lines = [f"run {run_id} · {usage.model_line} · {usage.samples} sample(s)"]
        lines.extend(_usage_rows(usage.by_agent, usage.turns_by_agent, usage.jobs_by_agent))
        lines.append(_usage_row("total", usage.total, turns=usage.samples))
        lines.append(_SPEND_NOT_REPORTED)
        return "\n".join(lines)

    def _tool_usage_today(self, args: dict[str, Any], by: str) -> str:
        # The same calendar day the run cap counts (loop.day_window), so the
        # two numbers on the last line always describe the same period.
        tz = self.config.daemon.run_cap_timezone
        since, _ = day_window(self.clock(), tz)
        try:
            runs = [r for r in self.store.list_runs() if r.updated_at >= since]
        except SbxloopError as exc:
            return f"cannot list runs: {_one_line(str(exc), 200)}"
        total = Usage()
        rows: dict[str, Usage] = {}
        row_turns: dict[str, int] = {}
        models: list[str] = []
        samples = 0
        for record in runs:
            usage = self._usage_for_run(record.run_id, since=since)
            if not usage.recorded:
                continue
            total = total.merged(usage.total)
            rows[record.run_id] = usage.total
            row_turns[record.run_id] = usage.samples
            models.extend(m for m in usage.models if m not in models)
            samples += usage.samples
        try:
            status = self.loop.status()
            cap = f"{status.get('runs_today', '?')}/{status.get('max_runs_per_day', '?')}"
        except Exception:
            cap = "?"
        if not rows:
            return (
                f"no usage recorded today ({tz}) across {len(runs)} run(s) "
                f"({cap} runs today). Nothing has been spent, or these runs predate "
                "usage reporting."
            )
        head = f"today ({tz}) · {len(rows)} run(s) with usage · {cap} runs today"
        if models:
            head += f" · {', '.join(models)}"
        lines = [head]
        lines.extend(_usage_rows(rows, row_turns))
        lines.append(_usage_row("total", total, turns=samples))
        lines.append(_SPEND_NOT_REPORTED)
        return "\n".join(lines)

    def _tool_daemon_log(self, args: dict[str, Any], by: str) -> str:
        tail = _int_arg(args, "tail", 50, 1, 500)
        level = str(args.get("level") or "").strip().upper() or None
        grep = str(args.get("grep") or "")
        buffer = log_buffer()
        size = len(buffer)
        try:
            records = buffer.tail(tail, level=level, grep=grep or None)
        except ValueError:
            return f"unknown log level {level!r} — use one of {', '.join(_LOG_LEVELS)}"
        filters: list[str] = []
        if level:
            filters.append(f"level={level}")
        if grep:
            filters.append(f"grep={grep!r}")
        suffix = f" ({', '.join(filters)})" if filters else ""
        if not records:
            return f"no matching log records{suffix}; the buffer holds {size} lines"
        head = f"showing {len(records)} of {size} buffered log records{suffix}"
        lines = [f"{r.timestamp} {r.level} {r.logger} {_one_line(r.line, 400)}" for r in records]
        return "\n".join([head, *lines])

    def _tool_github_get(self, args: dict[str, Any], by: str) -> str:
        assert self.github is not None
        repo = self.config.github.repo
        what = str(args.get("what", ""))
        number = args.get("number")
        path = args.get("path")
        ref = args.get("ref")
        if what in {"pr", "pr_files", "pr_diff", "issue", "issue_comments"} and not number:
            return f"{what} needs number"
        try:
            n = int(number) if number is not None else 0
        except (TypeError, ValueError):
            return f"number must be an integer, got {number!r}"
        try:
            if what == "pr":
                data = self.github.call(lambda ops: ops.raw("GET", f"/repos/{repo}/pulls/{n}"))
                return _pr_summary(data)
            if what in ("pr_files", "pr_diff"):
                files = self.github.call(
                    lambda ops: ops.raw("GET", f"/repos/{repo}/pulls/{n}/files?per_page=100")
                )
                return _pr_files(files, with_patch=what == "pr_diff")
            if what == "issue":
                data = self.github.call(lambda ops: ops.raw("GET", f"/repos/{repo}/issues/{n}"))
                return _issue_summary(data)
            if what == "issue_comments":
                data = self.github.call(
                    lambda ops: ops.raw("GET", f"/repos/{repo}/issues/{n}/comments?per_page=50")
                )
                return _issue_comments(data)
            if what == "file":
                if not path:
                    return "file needs path"
                assert repo is not None
                content = self.github.call(
                    lambda ops: ops.contents_read(repo, str(path), str(ref) if ref else None)
                )
                return f"{path}@{ref or 'default'}:\n{content}"
        except (GithubOpsError, WorkerError, SbxError, DaemonError) as exc:
            return f"GitHub read failed: {_one_line(str(exc), 300)}"
        return f"unknown what {what!r}"

    def _tool_pr_status(self, args: dict[str, Any], by: str) -> str:
        assert self.github is not None
        repo = self.config.github.repo
        number = args.get("number")
        if not number:
            return "pr_status needs number"
        try:
            n = int(number)
        except (TypeError, ValueError):
            return f"number must be an integer, got {number!r}"

        missing = f"PR #{n} does not exist in {repo}"
        try:
            pr = self.github.call(lambda ops: ops.raw("GET", f"/repos/{repo}/pulls/{n}"))
        except (GithubOpsError, WorkerError, SbxError, DaemonError) as exc:
            text = str(exc).lower()
            if "404" in text or "not found" in text:
                return missing
            return f"reading PR #{n} failed: {_one_line(str(exc), 300)}"
        if not isinstance(pr, dict) or not pr.get("number"):
            return missing

        head = pr.get("head") or {}
        base = pr.get("base") or {}
        sha = str(head.get("sha") or "")
        try:
            checks: Any = {}
            if sha:
                checks = self.github.call(
                    lambda ops: ops.raw("GET", f"/repos/{repo}/commits/{sha}/check-runs")
                )
            reviews = self.github.call(
                lambda ops: ops.raw("GET", f"/repos/{repo}/pulls/{n}/reviews?per_page=100")
            )
        except (GithubOpsError, WorkerError, SbxError, DaemonError) as exc:
            return f"reading PR #{n} failed: {_one_line(str(exc), 300)}"

        title = _one_line(str(pr.get("title") or ""), 120)
        flags = str(pr.get("state") or "?")
        if pr.get("draft"):
            flags = f"{flags}, draft"
        base_ref = _one_line(str(base.get("ref") or "?"), 60)
        head_ref = _one_line(str(head.get("ref") or "?"), 60)
        state = pr.get("mergeable_state")
        if not state:
            behind = f"behind-ness unknown (vs {base_ref})"
        elif str(state).lower() == "behind":
            behind = f"behind {base_ref}"
        else:
            behind = f"up to date with {base_ref}"

        lines = [
            _one_line(f"#{n} {title} [{flags}] {pr.get('html_url') or ''}".strip(), 300),
            _one_line(f"{head_ref} → {base_ref}", 200),
            _check_runs_summary(checks, limit=5),
            f"reviews: {_reviews_summary(reviews)}",
            _one_line(f"{_mergeable_summary(pr)} · {behind}", 300),
        ]
        return "\n".join(lines)[:2000]

    def _tool_create_issue(self, args: dict[str, Any], by: str) -> str:
        assert self.github is not None
        repo = self.config.github.repo
        assert repo is not None
        title = _one_line(str(args.get("title", "")).strip(), 200)
        body = str(args.get("body", "")).strip()
        if not title or not body:
            return "both title and body are required"
        backlog = self.config.daemon.backlog_label
        trigger = self.config.daemon.trigger_label
        full_body = f"{body}\n\n---\nFiled by {by} via the sbxloop concierge\n"
        try:
            ref = self.github.call(
                lambda ops: ops.issue_create(repo, title, full_body, labels=[backlog])
            )
        except (GithubOpsError, WorkerError, SbxError, DaemonError) as exc:
            return f"creating the issue failed: {_one_line(str(exc), 300)}"
        log.info("concierge.issue_created", number=ref.number, by=by, title=title[:80])
        return (
            f"created issue #{ref.number} {ref.url} with the `{backlog}` label. It will NOT "
            f"run until it carries `{trigger}` — ask the person whether to add it, and call "
            f"label_issue_for_run({ref.number}) only if they say yes."
        )

    def _tool_list_issues(self, args: dict[str, Any], by: str) -> str:
        assert self.github is not None
        repo = self.config.github.repo
        daemon = self.config.daemon
        limit = _int_arg(args, "limit", 20, 1, 50)
        label = str(args.get("label") or "").strip()
        if not label and not args.get("all"):
            label = daemon.backlog_label
        query = f"state=open&per_page={limit}&sort=updated&direction=desc"
        if label:
            query += f"&labels={quote(label, safe='')}"
        try:
            data = self.github.call(lambda ops: ops.raw("GET", f"/repos/{repo}/issues?{query}"))
        except (GithubOpsError, WorkerError, SbxError, DaemonError) as exc:
            return f"listing issues failed: {_one_line(str(exc), 300)}"
        if not isinstance(data, list):
            return json.dumps(data, default=str)[:2000]
        issues = [d for d in data if isinstance(d, dict) and "pull_request" not in d]
        if not issues:
            return f"no open issues in {repo}" + (f" with label `{label}`" if label else "")
        now = self.clock()
        lines = [
            f"{len(issues)} open issue(s) in {repo}"
            + (f" with `{label}`" if label else "")
            + f" (newest activity first, max {limit}):"
        ]
        for issue in issues:
            labels = _label_names(issue)
            flags = []
            if daemon.trigger_label in labels:
                flags.append("QUEUED for a run")
            if daemon.in_progress_label in labels:
                flags.append("RUNNING")
            if daemon.failed_label in labels:
                flags.append("failed before")
            created = _iso_age(str(issue.get("created_at") or ""), now)
            user = (issue.get("user") or {}).get("login", "?")
            lines.append(
                f"- #{issue.get('number')} {_one_line(str(issue.get('title') or ''), 100)} · "
                f"[{', '.join(labels) or 'no labels'}] · {created} old · by {user} · "
                f"{issue.get('comments', 0)} comments · {issue.get('html_url')}"
                + (f" · {' / '.join(flags)}" if flags else "")
            )
        lines.append(
            f"Issues without `{daemon.trigger_label}` are not queued: ask the person which, "
            "if any, should be worked, and label_issue_for_run those they name."
        )
        return "\n".join(lines)

    def _tool_label_issue_for_run(self, args: dict[str, Any], by: str) -> str:
        assert self.github is not None
        repo = self.config.github.repo
        number = _issue_number(args)
        if number <= 0:
            return "number is required"
        trigger = self.config.daemon.trigger_label
        try:
            self.github.call(
                lambda ops: ops.raw(
                    "POST", f"/repos/{repo}/issues/{number}/labels", {"labels": [trigger]}
                )
            )
        except (GithubOpsError, WorkerError, SbxError, DaemonError) as exc:
            return f"labelling #{number} failed: {_one_line(str(exc), 300)}"
        log.info("concierge.issue_labelled_for_run", number=number, by=by, label=trigger)
        return (
            f"added `{trigger}` to #{number} — the daemon claims it on its next poll "
            f"(every {self.config.daemon.poll_interval_s:g}s) and runs it after anything "
            "already queued."
        )

    def _tool_comment_on_issue(self, args: dict[str, Any], by: str) -> str:
        """Answer an issue in place. Same attribution trailer as
        ``create_issue``: the comment arrives under the bot's token, so the
        trailer is the only thing saying which human it came from."""
        assert self.github is not None
        repo = self.config.github.repo
        assert repo is not None
        number = _issue_number(args)
        if number <= 0:
            return "number is required"
        body = str(args.get("body", "")).strip()
        if not body:
            return "body is required"
        full_body = f"{body}\n\n---\nPosted by {by} via the sbxloop concierge\n"
        try:
            url = self.github.call(lambda ops: ops.issue_comment(repo, number, full_body))
        except (GithubOpsError, WorkerError, SbxError, DaemonError) as exc:
            return f"commenting on #{number} failed: {_one_line(str(exc), 300)}"
        log.info("concierge.issue_commented", number=number, by=by, chars=len(body))
        return f"commented on #{number}" + (f" — {url}" if url else "")

    def _tool_close_issue(self, args: dict[str, Any], by: str) -> str:
        """Triage's last step, and the one concierge action that is *not*
        direct: a close is outward-facing and carries a judgement the person
        who filed the issue reads, so it happens only after an explicit yes
        (``confirmation``, required — see the prompt). The daemon closes the
        issues it finishes elsewhere (``[daemon] close_on_success``); this is
        for duplicates, won't-fixes and stale items.

        The issue is read first so the result can name what was closed, and
        so three cases never become a write: a pull-request number, an
        already-closed issue, and one a run is working on right now — closing
        that would not stop the microVM. Each mutation is its own
        :meth:`DaemonGithub.call`, because ``call`` replays its lambda once
        after dropping a dead sandbox and a replayed comment is a duplicate.
        """
        assert self.github is not None
        repo = self.config.github.repo
        assert repo is not None
        number = _issue_number(args)
        if number <= 0:
            return "number is required"
        reason = str(args.get("reason", "")).strip()
        if reason not in CLOSE_REASONS:
            return f"reason must be one of {', '.join(CLOSE_REASONS)}, not {reason!r}"
        confirmation = _one_line(str(args.get("confirmation", "")), 200)
        if not confirmation:
            return (
                f"close_issue needs the person's own words agreeing that #{number} should be "
                "closed. Ask them — naming the issue and what will happen — and pass what "
                "they answered as `confirmation`."
            )
        comment = str(args.get("comment", "")).strip()
        daemon = self.config.daemon
        path = f"/repos/{repo}/issues/{number}"
        try:
            data = self.github.call(lambda ops: ops.raw("GET", path))
        except (GithubOpsError, WorkerError, SbxError, DaemonError) as exc:
            return f"reading #{number} failed, so it was not closed: {_one_line(str(exc), 300)}"
        if not isinstance(data, dict):
            return f"#{number} did not come back as an issue: {_one_line(str(data), 200)}"
        if "pull_request" in data:
            return f"#{number} is a pull request, not an issue — close_issue only closes issues."
        title = _one_line(str(data.get("title") or ""), 100)
        url = str(data.get("html_url") or "")
        if str(data.get("state")) == "closed":
            was = str(data.get("state_reason") or "no reason recorded")
            return f'#{number} "{title}" is already closed ({was}) — nothing to do. {url}'
        labels = _label_names(data)
        item = self.dstore.get(f"gh:{number}")
        running = item is not None and item.state == "running"
        if daemon.in_progress_label in labels or running:
            run = f" (run `{item.run_id}`)" if item is not None and item.run_id else ""
            return (
                f'#{number} "{title}" is being worked right now{run} — closing it would not '
                "stop the run. Cancel that first (`sbx_control` with `cancel`), then close it."
            )
        notes: list[str] = []
        if comment:
            body = f"{comment}\n\n---\nClosed as {reason} by {by} via the sbxloop concierge\n"
            try:
                self.github.call(lambda ops: ops.issue_comment(repo, number, body))
            except (GithubOpsError, WorkerError, SbxError, DaemonError) as exc:
                return (
                    f"commenting on #{number} failed, so it was NOT closed: "
                    f"{_one_line(str(exc), 300)}"
                )
            notes.append("posted the reason as a comment")
        if daemon.trigger_label in labels:
            # Removing it before the close also shuts the claim window: a poll
            # landing mid-sequence declines on either signal (sources.claim).
            try:
                self.github.call(lambda ops: _remove_label(ops, path, daemon.trigger_label))
            except (GithubOpsError, WorkerError, SbxError, DaemonError) as exc:
                notes.append(
                    f"could NOT remove `{daemon.trigger_label}` ({_one_line(str(exc), 120)}) — "
                    "reopening it would queue a run"
                )
            else:
                notes.append(f"removed `{daemon.trigger_label}`")
        try:
            self.github.call(
                lambda ops: ops.raw("PATCH", path, {"state": "closed", "state_reason": reason})
            )
        except (GithubOpsError, WorkerError, SbxError, DaemonError) as exc:
            done = f" (already done: {', '.join(notes)})" if notes else ""
            return f"closing #{number} failed: {_one_line(str(exc), 300)}{done}"
        log.info(
            "concierge.issue_closed",
            number=number,
            reason=reason,
            by=by,
            confirmation=confirmation,
            commented=bool(comment),
        )
        item_note = _work_item_note(item)
        if item_note:
            notes.append(item_note)
        tail = "\n" + "\n".join(f"- {note}" for note in notes) if notes else ""
        return f'closed #{number} "{title}" as {reason}' + (f" — {url}" if url else "") + tail

    # -- helpers ------------------------------------------------------------------

    @property
    def store(self) -> StateStore:
        if self._store is None:
            self._store = self._store_factory()
        return self._store


# -- module helpers ---------------------------------------------------------------


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _issue_number(args: dict[str, Any]) -> int:
    """The ``number`` argument as a positive int; 0 for missing or unparseable."""
    try:
        number = int(args.get("number", 0))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _label_names(data: Any) -> list[str]:
    """The label names on an issue payload, ignoring anything malformed."""
    if not isinstance(data, dict):
        return []
    return [str(lb.get("name")) for lb in data.get("labels") or [] if isinstance(lb, dict)]


def _remove_label(ops: GithubOps, issue_path: str, label: str) -> None:
    """DELETE a label, treating "it was not there" (404 on the label
    resource) as success — same tolerance as ``GitHubIssueSource``. Swallowed
    inside the ``DaemonGithub.call`` lambda so a 404 never looks like a dead
    sandbox and triggers its drop-and-retry."""
    try:
        ops.raw("DELETE", f"{issue_path}/labels/{quote(label, safe='')}")
    except GithubOpsError as exc:
        missing = exc.http_status == 404 if exc.http_status is not None else "HTTP 404" in str(exc)
        if not missing:
            raise


def _work_item_note(item: WorkItem | None) -> str:
    """Closing an issue does not remove the daemon's own work item, and the
    two cases differ sharply. An **unclaimed** item is harmless: the loop
    calls ``source.claim`` before dispatching, which re-reads the issue and
    declines a closed one (``sources.py``), so the item is merely dropped. An
    **already-claimed** one skips that check entirely (``loop.py``: ``if not
    item.claimed``) and can still start a whole run on the issue that was
    just closed — which is worth saying out loud."""
    if item is None or item.state not in ("queued", "failed"):
        return ""
    if not item.claimed:
        return (
            f"the daemon still lists `{item.item_id}` as {item.state}, but it re-checks that "
            "the issue is open before starting, so nothing will run — it reports the item as "
            "dropped on its next poll"
        )
    return (
        f"WARNING: the daemon still holds `{item.item_id}` ({item.state}, already claimed) and "
        "closing the issue does NOT stop it — a run can still start. Say so, and use "
        f"`sbx_control` with `abandon {item.item_id} issue closed` if it should be dropped"
    )


def _int_arg(args: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(args.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


class _RunUsage(NamedTuple):
    """One run's folded ``agent.usage`` samples."""

    total: Usage
    by_agent: dict[str, Usage]
    models: list[str]
    samples: int
    # Turns and distinct jobs per persona. ``samples`` is the run's total
    # turn count (one sample per turn); these break it down so a persona
    # that is expensive because it takes many turns is distinguishable from
    # one that is expensive because it runs many times. Required rather than
    # defaulted: a shared mutable default on a NamedTuple is a trap, and the
    # single producer always has both to hand.
    turns_by_agent: dict[str, int]
    jobs_by_agent: dict[str, int]

    @property
    def recorded(self) -> bool:
        """Did the backend actually report anything? ``Usage.merged`` keeps
        None as None, so an all-None total means "never reported" — which is
        not the same as zero and must not be shown as it."""
        return self.samples > 0 and (
            self.total.input_tokens is not None or self.total.output_tokens is not None
        )

    @property
    def model_line(self) -> str:
        return " + ".join(self.models) if self.models else "model not reported"


# `agent.usage` payloads carry an `agent` key the host adds on the way through
# (worker/client.py), and Usage forbids extras — so pick the fields out rather
# than validating the whole dict.
#
# Spend is deliberately absent (#386, #439): the Copilot SDK reports it as a
# per-turn constant (15.0 on every turn) of unknown unit, so lifting it out of
# the event and folding it through `Usage.merged` made `run_usage` print
# 147 x 15.0 = 2205.0 — a fabricated figure the concierge would repeat in
# Discord as fact. `Usage` carries no such field at all now.
_USAGE_FIELDS = (
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def _usage_from_event(data: dict[str, Any]) -> Usage:
    return Usage(**{k: data[k] for k in _USAGE_FIELDS if k in data})


def _tokens(value: int | None) -> str:
    return f"{value:,}" if value is not None else "—"


def _usage_row(
    label: str, usage: Usage, *, turns: int | None = None, jobs: int | None = None
) -> str:
    """One spend line, with the turn/job and cache columns appended only when
    they are known — a run that predates turn counting or a backend that
    reports no cache figures must not grow empty columns."""
    row = (
        f"{label:<14} {_tokens(usage.input_tokens):>11} in · {_tokens(usage.output_tokens):>9} out"
    )
    if turns is not None:
        row += f" · {turns} turn{'s' if turns != 1 else ''}"
        if jobs:
            row += f"/{jobs} job{'s' if jobs != 1 else ''}"
    if usage.cache_read_tokens is not None:
        row += f" · {_tokens(usage.cache_read_tokens)} cached"
    return row


def _usage_rows(
    rows: dict[str, Usage],
    turns: dict[str, int] | None = None,
    jobs: dict[str, int] | None = None,
) -> list[str]:
    """Biggest spender first — the answer to "where did it go?" is the top line."""
    ordered = sorted(
        rows.items(), key=lambda kv: -((kv[1].input_tokens or 0) + (kv[1].output_tokens or 0))
    )
    return [
        _usage_row(
            label,
            usage,
            turns=None if turns is None else turns.get(label, 0),
            jobs=None if jobs is None else jobs.get(label, 0),
        )
        for label, usage in ordered
    ]


# No backend reports a spend figure in a known unit, so every usage block ends
# with the same plain statement rather than a number. The concierge repeats
# this line in Discord as fact, and a zero or a fabricated figure would be
# repeated just as confidently (#386, #439).
_SPEND_NOT_REPORTED = "spend: not reported by the agent backend (tokens above are the whole record)"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 14)].rstrip() + "\n… (truncated)"


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: max(0, limit - 1)].rstrip() + "…"


def _age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _iso_age(stamp: str, now: float) -> str:
    """``2026-08-17T05:35:00Z`` → ``3d`` (or ``?`` for anything unparseable)."""
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return "?"
    return _age(now - then)


def _status_detail(status: dict[str, Any]) -> str:
    """The ``status`` fields its reply text does not spell out, as prose.

    The concierge answers the channel in chat markdown, so it is handed
    prose rather than the raw status dict — a JSON blob in a tool result is
    a JSON blob the model may paste into Discord, next to the same numbers
    it just wrote in words.
    """
    current = status.get("current") or {}
    bits: list[str] = []
    if current.get("item_id"):
        bits.append(f"current work item: {current['item_id']}")
    bits.append(f"consecutive failures: {status.get('consecutive_failures', 0)}")
    if status.get("stopping"):
        bits.append("the daemon is shutting down")
    return " · ".join(bits)


def _looks_like_lost_session(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "session" in text and any(
        word in text for word in ("not found", "unknown", "expired", "no such", "does not exist")
    )


def _pr_summary(data: Any) -> str:
    if not isinstance(data, dict):
        return json.dumps(data, default=str)[:2000]
    head = data.get("head") or {}
    base = data.get("base") or {}
    return "\n".join(
        [
            f"PR #{data.get('number')}: {data.get('title')}",
            f"state: {data.get('state')}{' (merged)' if data.get('merged') else ''} · "
            f"draft: {data.get('draft')} · mergeable: {data.get('mergeable')}",
            f"{head.get('ref')} → {base.get('ref')} · +{data.get('additions')} "
            f"-{data.get('deletions')} in {data.get('changed_files')} files",
            f"url: {data.get('html_url')}",
            f"body: {_one_line(str(data.get('body') or ''), 1500)}",
        ]
    )


def _pr_files(files: Any, *, with_patch: bool) -> str:
    if not isinstance(files, list):
        return json.dumps(files, default=str)[:2000]
    lines = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        lines.append(
            f"{entry.get('status')} {entry.get('filename')} "
            f"(+{entry.get('additions')} -{entry.get('deletions')})"
        )
        if with_patch and entry.get("patch"):
            patch = str(entry["patch"])
            if len(patch) > 1500:
                patch = patch[:1500] + "\n… (patch truncated)"
            lines.append(patch)
    return "\n".join(lines) or "(no files)"


def _check_runs_summary(data: Any, *, limit: int = 10) -> str:
    """The ``check-runs`` payload as counts plus the runs worth clicking.

    Accepts either the raw ``{"check_runs": [...]}`` dict or a bare list,
    and ignores anything in it that is not a dict.
    """
    runs = data.get("check_runs") if isinstance(data, dict) else data
    if not isinstance(runs, list):
        return "(no checks)"
    entries = [r for r in runs if isinstance(r, dict)]
    if not entries:
        return "(no checks)"

    counts: dict[str, int] = {}
    interesting: list[str] = []
    for run in entries:
        conclusion = run.get("conclusion")
        if conclusion:
            state = str(conclusion).lower()
        else:
            state = str(run.get("status") or "pending").lower()
        counts[state] = counts.get(state, 0) + 1
        if state == "success":
            continue
        name = _one_line(str(run.get("name") or "(unnamed)"), 120)
        url = run.get("html_url")
        line = f"- {name}: {state}"
        if url:
            line = f"{line} — {_one_line(str(url), 200)}"
        interesting.append(_one_line(line, 200))

    order = [
        "success",
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "neutral",
        "skipped",
    ]
    ranked = sorted(counts, key=lambda s: (order.index(s) if s in order else len(order), s))
    words = {"success": "passed", "failure": "failed"}
    head = ", ".join(f"{counts[state]} {words.get(state, state)}" for state in ranked)
    total = len(entries)
    lines = [f"{total} check{'s' if total != 1 else ''}: {head}"]
    lines.extend(interesting[:limit])
    if len(interesting) > limit:
        lines.append(f"… ({len(interesting) - limit} more)")
    return "\n".join(lines)


def _reviews_summary(data: Any, *, limit: int = 10) -> str:
    """Latest review state per reviewer, e.g. ``APPROVED by alice``."""
    if not isinstance(data, list):
        return "no reviews"
    latest: dict[str, str] = {}
    for review in data:
        if not isinstance(review, dict):
            continue
        user = review.get("user")
        login = str((user or {}).get("login") or "?") if isinstance(user, dict) else "?"
        state = str(review.get("state") or "").upper()
        if not state:
            continue
        if state in ("COMMENTED", "PENDING") and latest.get(login) in (
            "APPROVED",
            "CHANGES_REQUESTED",
            "DISMISSED",
        ):
            continue
        latest[login] = state
    if not latest:
        return "no reviews"
    shown = list(latest.items())[:limit]
    text = ", ".join(f"{state} by {login}" for login, state in shown)
    if len(latest) > limit:
        text = f"{text}, … ({len(latest) - limit} more)"
    return _one_line(text, 300)


def _mergeable_summary(data: Any) -> str:
    """``mergeable``/``mergeable_state`` as a short phrase."""
    if not isinstance(data, dict):
        return "mergeable state unavailable"
    mergeable = data.get("mergeable")
    if mergeable is True:
        text = "mergeable"
    elif mergeable is False:
        text = "not mergeable"
    else:
        text = "mergeable state unknown (GitHub still computing)"
    state = data.get("mergeable_state")
    if state:
        state = _one_line(str(state), 60)
        text = f"{text} ({state})"
        if state.lower() == "behind":
            text = f"{text} — branch is behind base"
    return text


def _issue_summary(data: Any) -> str:
    if not isinstance(data, dict):
        return json.dumps(data, default=str)[:2000]
    labels = ", ".join(_label_names(data))
    return "\n".join(
        [
            f"issue #{data.get('number')}: {data.get('title')} [{data.get('state')}]",
            f"labels: {labels or '(none)'} · comments: {data.get('comments')}",
            f"url: {data.get('html_url')}",
            f"body: {_one_line(str(data.get('body') or ''), 2000)}",
        ]
    )


def _issue_comments(data: Any) -> str:
    if not isinstance(data, list):
        return json.dumps(data, default=str)[:2000]
    lines = []
    for comment in data:
        if not isinstance(comment, dict):
            continue
        user = (comment.get("user") or {}).get("login", "?")
        body = _one_line(str(comment.get("body") or ""), 400)
        lines.append(f"- {user} ({comment.get('created_at')}): {body}")
    return "\n".join(lines) or "(no comments)"
