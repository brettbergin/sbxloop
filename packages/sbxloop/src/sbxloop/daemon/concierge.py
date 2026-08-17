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
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from sbxloop.config import Config
from sbxloop.daemon.control import dispatch, plain
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.prompts import bullet_list, render
from sbxloop.engine.store import StateStore
from sbxloop.errors import (
    DaemonError,
    ProvisionError,
    SbxError,
    SbxloopError,
    WorkerError,
    WorkerTimeoutError,
)
from sbxloop.events import EventBus
from sbxloop.ids import new_job_id
from sbxloop.log import get_logger
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import HostToolCall, HostToolResponse, HostToolSpec, JobRequest

if TYPE_CHECKING:
    from sbxloop.daemon.github import DaemonGithub
    from sbxloop.daemon.loop import DaemonLoop
    from sbxloop.daemon.sources import InboxSource

log = get_logger(__name__)

# Persona stamped on the concierge's agent.* events (transcript attribution).
CONCIERGE_AGENT = "concierge"
# The events of a concierge turn carry this run id.
CONCIERGE_RUN_ID = "concierge"
# daemon_state keys
STATE_SESSION_ID = "concierge_session_id"
STATE_SESSION_TURNS = "concierge_session_turns"


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
            "runs per day; a consecutive-failure breaker pauses dispatch",
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
            f"{'open' if status.get('breaker_open') else 'closed'} | runs today: "
            f"{status.get('runs_today', 0)}/{status.get('max_runs_per_day', '?')} | "
            f"speaker: {author}"
        )

    # -- tools ---------------------------------------------------------------

    def _tool_handler(self, call: HostToolCall, *, author: str) -> HostToolResponse:
        tool = self._tools.get(call.name)
        if tool is None:
            return HostToolResponse(
                call_id=call.call_id, ok=False, error=f"unknown tool {call.name!r}"
            )
        by = f"{author} (via concierge)"
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
        return [
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
        ]

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
            text += "\n" + json.dumps(reply.status, default=str)
        if not reply.ok:
            text = f"(command not accepted) {text}"
        return text

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


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 14)].rstrip() + "\n… (truncated)"


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: max(0, limit - 1)].rstrip() + "…"


def _looks_like_lost_session(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "session" in text and any(
        word in text for word in ("not found", "unknown", "expired", "no such", "does not exist")
    )
