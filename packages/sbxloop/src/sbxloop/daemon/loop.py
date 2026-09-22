"""DaemonLoop: discover → claim → run → settle, forever.

One item at a time, one fresh :class:`LoopEngine` per item (engines are
single-use: their cancel flag never clears), one shared daemon-owned
:class:`StateStore`, a fresh :class:`EventBus` per run (each engine adds
permanent subscribers to its bus). The engine carries the item all the way
— task graph, gate, pull request, its own review, fix rounds, CI, merge —
so the daemon's whole job is to hand it an issue and settle on how the run
ended: ``merged`` closes the issue, ``failed`` retries or gives up,
``blocked`` hands the PR to a human. The daemon never files work of its
own.

Spend guardrails — a calendar-day run cap that counts runs started since
00:00 in ``daemon.run_cap_timezone`` (default ``UTC``) and resets at the
next midnight there; a per-item attempt cap; a consecutive-failure circuit
breaker — are the daemon's only defense against a mislabeled issue in a
fully autonomous setup, so they are enforced in the tick, not left to
configuration hope.

Shutdown is cooperative: a signal sets the stop flag, asks the in-flight
engine to cancel (honored at its next boundary), and joins it briefly.
Interrupted runs are resumable by design, so the item stays ``running``;
:meth:`recover` re-queues it with the run pinned on the next start and the
tick resumes it through the same guardrails as any dispatch.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, Protocol, cast
from zoneinfo import ZoneInfo

from sbxloop import __version__, hostgit
from sbxloop.agents.assignment import (
    AgentAssignment,
    MemoryBlocks,
    RunRole,
    plan_assignment,
)
from sbxloop.agents.chronicle import RunChronicle
from sbxloop.agents.memory import MemoryService, WorkspaceChannelVisibility
from sbxloop.agents.posts import ChannelPoster, RunArtifacts
from sbxloop.agents.registry import AgentRegistry, DbAgentRegistry
from sbxloop.config import (
    VCS_KINDS,
    Config,
    GithubConfig,
    RepoConfig,
    SandboxConfig,
    ScheduleConfig,
    VcsKind,
)
from sbxloop.daemon.controls.eligibility import Subject, check as check_eligibility
from sbxloop.daemon.controls.generation import (
    GENERATION_KEY,
    GENERATION_STARTED_KEY,
    new_generation_id,
)
from sbxloop.daemon.controls.operations import OperationStore, reconcile_operations
from sbxloop.daemon.controls.principal import Principal
from sbxloop.daemon.controls.results import (
    CancelOutcome,
    ControlError,
    ResumeOutcome,
    SteerOutcome,
)
from sbxloop.daemon.github import DaemonGithub
from sbxloop.daemon.holds import OPERATOR_HOLD, hold_name
from sbxloop.daemon.logsink import event_log_subscriber
from sbxloop.daemon.model import (
    DaemonNotice,
    NoticeKind,
    NoticeLevel,
    RunReport,
    TaskOutcome,
    TickOutcome,
    TickResult,
    WorkItem,
    is_planned_assignment,
    requested_roles,
)
from sbxloop.daemon.repositories import RepositoryRegistry
from sbxloop.daemon.schedule import Cadence, ScheduleRow, format_due
from sbxloop.daemon.sources import HIDDEN_MARKER_RE, IssueContext, WorkSource
from sbxloop.daemon.store import DaemonStore, MergeGate, ReviewHold
from sbxloop.daemon.usagepool import UsagePool, fairness_key
from sbxloop.db.event_scope import channel_for_item, channel_for_run
from sbxloop.engine.checks import check_policy_reader
from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.followups import FollowupFiler, recorded_review_rounds
from sbxloop.engine.landing import (
    UNKNOWN_IDENTITY,
    AwaitingReview,
    Blocked,
    Closed,
    Landed,
    LandingOutcome,
    LoopIdentity,
    NeedsFix,
    UpdateState,
    land,
    resolve_identity,
)
from sbxloop.engine.model import (
    RESUMABLE_RUN_STATES,
    TERMINAL_RUN_STATES,
    RunKind,
    RunRecord,
    RunResult,
    RunState,
    TaskRecord,
    run_summary,
)
from sbxloop.engine.reconcile import acknowledge_human_threads
from sbxloop.engine.sinks import published_line
from sbxloop.engine.store import StateStore
from sbxloop.errors import (
    ProvisionError,
    RunCancelledError,
    SbxError,
    SbxloopError,
    StateError,
)
from sbxloop.events import Event, EventBus, HostEventTypes
from sbxloop.gc import DAY_S, format_bytes, prune_run_dirs, workspace_pruned
from sbxloop.ghids import (
    is_api_id,
    is_chat_id,
    is_local_id,
    normalize_item_id,
    parse_schedule_id,
    schedule_item_id,
    try_parse_gh_id,
)
from sbxloop.ids import new_run_id
from sbxloop.log import bind_run, clear_run, get_logger
from sbxloop.provider import ProviderHeldError, ProviderRecovery
from sbxloop.recipes import get_recipe
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxRole
from sbxloop.sbx.provision import sandbox_name_candidates
from sbxloop.sbx.prune import remove_run_sandbox, remove_run_sandbox_secrets
from sbxloop.sbx.warm import Warmer

log = get_logger(__name__)

#: The ``daemon_state`` key an operator's `restart` leaves for the next
#: process (#969): who asked, why, when, and how.
RESTART_MARKER_KEY = "restart_requested"
UNSUPERVISED_REFUSAL = (
    "this daemon is not under a service manager that would start it again (no systemd "
    "unit around it, and `[daemon] supervised` is false) — `stop` and start it yourself, "
    "or set `[daemon] supervised = true` if something does"
)

# Hidden markers sbxloop leaves in issue bodies and comments; they are
# bookkeeping, not part of the outcome the agent should read.
_MARKER_RE = HIDDEN_MARKER_RE

# What an operator reading this failure needs to do about it. Static prose,
# so it is the one part of an error record that can be forwarded to an error
# reporter (``[telemetry] log_fields``) whatever else the record holds.
_BREAKER_HINT = (
    "`[daemon] max_consecutive_failures` runs failed in a row, so dispatch is "
    "paused for `breaker_cooldown_s` and one probe run follows; a breaker that "
    "keeps opening is usually the host, the agent credential or the base branch "
    "rather than any one item — `sbxloop daemon ctl status` says what is held"
)


@contextmanager
def defer_signals(signals: Sequence[int] = (signal.SIGINT, signal.SIGTERM)) -> Iterator[None]:
    """Hold SIGINT/SIGTERM for the duration of the block, then deliver them.

    The cleanup registry's handler quiesces and then raises ``SystemExit``
    on the main thread — which, arriving between "claim comment posted"
    and "claim persisted", is exactly the kill that orphaned #527 (#530).
    A claim is seconds of GitHub calls; holding the signal that long costs
    nothing and makes the claim atomic. Only the main thread may install
    handlers: anywhere else this is a no-op.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    pending: list[tuple[int, Any]] = []
    previous: dict[int, Any] = {}

    def hold(signum: int, frame: Any) -> None:
        log.info("signals.deferred", signal=signal.Signals(signum).name, hint="claim in flight")
        pending.append((signum, frame))

    for signum in signals:
        with suppress(ValueError, OSError):
            previous[signum] = signal.signal(signum, hold)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        for signum, frame in pending:
            handler = previous.get(signum)
            if callable(handler):
                handler(signum, frame)
            elif signum == signal.SIGINT:
                raise KeyboardInterrupt
            else:
                raise SystemExit(128 + signum)


def day_window(now: float, tz: str) -> tuple[float, float]:
    """The calendar day containing epoch ``now`` in IANA zone ``tz``, as
    ``(start_epoch, next_start_epoch)``.

    Every instant in the same local calendar date maps to the same
    ``start_epoch``, and the count only resets when local midnight passes.
    ``next_start_epoch`` is the next local midnight (which is not always
    86400s later — DST days are 23 or 25 hours long)."""
    zone = ZoneInfo(tz)
    local = datetime.fromtimestamp(now, tz=zone)
    start = datetime.combine(local.date(), dtime(0, 0), tzinfo=zone)
    next_start = datetime.combine(local.date() + timedelta(days=1), dtime(0, 0), tzinfo=zone)
    return start.timestamp(), next_start.timestamp()


class Frontend(Protocol):
    """What a human-facing channel (Discord or Slack) sees of the loop's lifecycle.
    Every call is best-effort: the loop never depends on a frontend."""

    def daemon_notice(self, notice: DaemonNotice) -> None: ...
    def run_started(
        self, item: WorkItem, run_id: str, engine: LoopEngine, bus: EventBus
    ) -> None: ...
    def run_finished(self, item: WorkItem, report: RunReport) -> None: ...
    def merge_gate_opened(self, item: WorkItem, run_id: str, gate: MergeGate) -> None: ...
    def merge_gate_resolved(
        self,
        item: WorkItem,
        run_id: str,
        gate: MergeGate,
        outcome: str,
        by: str | None,
        detail: str | None = None,
    ) -> None: ...


class CancelRequest(NamedTuple):
    """An operator's ``!sbx cancel`` for one specific run. Recorded so the
    settle step can tell it from a failure: the engine surfaces both as an
    exception at the next boundary (field: a Discord cancel was settled as
    a failed attempt, re-run fresh after the backoff and counted toward the
    breaker — #246)."""

    run_id: str
    requester: str
    retry: bool
    #: The durable operation the cancel was recorded under, finished when
    #: the run settles (``None`` for a cancel the loop raised itself).
    operation_id: str | None = None


class MentionTarget(NamedTuple):
    """A live run an ``@agent`` mention could be about, and the tasks in it
    bound to that agent which are still in flight (S-A11)."""

    run_id: str
    task_ids: tuple[str, ...]


class RunHandle:
    """A run in flight: what shutdown, a control and a frontend need to
    reach, and what the loop needs to settle it once its thread ends."""

    def __init__(
        self,
        item: WorkItem,
        run_id: str,
        engine: LoopEngine,
        bus: EventBus,
        *,
        resume: bool = False,
    ) -> None:
        self.item = item
        self.run_id = run_id
        self.engine = engine
        self.bus = bus
        self.resume = resume
        self.started = time.monotonic()
        # The engine thread and what it left behind: ``result`` or ``error``.
        self.thread: threading.Thread | None = None
        self.outcome: dict[str, Any] = {}
        # An operator override (a row changed by another process) was seen
        # and the engine asked to stop; asked once.
        self.override_cancel_sent = False

    @property
    def finished(self) -> bool:
        """The engine thread ran and has ended: the run is ready to settle."""
        return self.thread is not None and not self.thread.is_alive()

    def snapshot(self) -> dict[str, Any]:
        return {
            "item_id": self.item.item_id,
            "run_id": self.run_id,
            "title": self.item.title,
            # A workload and its profile (#804): the console's "now"
            # line says which bounds the run is under.
            "kind": self.item.kind,
            "profile": self.item.profile,
        }


# (item, per-item config, run_id, bus, resume) -> RunResult. Injectable so
# the tick algorithm is testable without sandboxes; the default builds a
# fresh LoopEngine and calls start() or resume().
Runner = Callable[[WorkItem, Config, str, EventBus, bool], RunResult]


def _render_context(context: IssueContext) -> str:
    """The discussion and linked-issue blocks as the outcome carries them."""
    blocks: list[str] = []
    if context.comments or context.omitted:
        total = len(context.comments) + context.omitted
        noun = "comment" if total == 1 else "comments"
        lines = [f"## Discussion ({total} {noun})"]
        if context.omitted:
            earlier = "comment" if context.omitted == 1 else "comments"
            lines.append(f"({context.omitted} earlier {earlier} omitted; the latest are shown)")
        for comment in context.comments:
            when = f" ({comment.created})" if comment.created else ""
            lines.append(f"**@{comment.author}**{when}: {comment.body}")
        blocks.append("\n\n".join(lines))
    if context.linked:
        lines = ["## Linked issues"]
        for linked in context.linked:
            state = linked.state if linked.kind == "issue" else f"{linked.state} {linked.kind}"
            line = f"- #{linked.number} ({state}) — {linked.title}"
            if linked.excerpt:
                line += f": {linked.excerpt}"
            lines.append(line)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _fit_context(block: str, room: int, limit: int) -> str:
    """``block`` cut to ``room`` characters with a note naming the budget;
    whole when it fits. The note itself is what remains when there is no
    room at all — the agent is told the discussion exists either way."""
    if len(block) <= room:
        return block
    note = (
        f"(discussion clipped by [budgets] outcome_max_chars={limit}: "
        "{hidden} chars not shown — the issue on GitHub has the rest)"
    )
    keep = max(0, room - len(note.format(hidden=len(block))) - 2)
    kept = block[:keep].rstrip()
    hidden = len(block) - len(kept)
    return f"{kept}\n\n{note.format(hidden=hidden)}" if kept else note.format(hidden=hidden)


def _item_assignment(item: WorkItem) -> AgentAssignment | None:
    """The planned assignment ``item`` carries; None before one is planned."""
    if not is_planned_assignment(item.assignment_json):
        return None
    assert item.assignment_json is not None  # nosec B101 - checked above
    return AgentAssignment.from_json(item.assignment_json)


class DaemonLoop:
    def __init__(
        self,
        config: Config,
        *,
        store: StateStore,
        dstore: DaemonStore,
        source: WorkSource,
        sbx: SbxCLI | None = None,
        runner: Runner | None = None,
        clock: Callable[[], float] = time.time,
        frontend: Frontend | None = None,
        github: DaemonGithub | None = None,
        worker_python: str | None = None,
        install_workers: bool | None = None,
        poster: ChannelPoster | None = None,
        repositories: RepositoryRegistry | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.dstore = dstore
        self.source = source
        self.sbx = sbx
        self.clock = clock
        self.started_at = clock()
        self.frontend = frontend
        # How a run linked to a channel posts into it (S-P17). The API
        # listener supplies one; a daemon without the API has none, and a
        # run then reports through its events alone.
        self.poster = poster
        # The daemon's own gh-ops box: what the gate-approve path merges
        # with. None only in tests that never approve.
        self.github = github
        # Engine construction knobs (tests/e2e point these at the host
        # interpreter and skip the install ladder, like the CLI does).
        self.worker_python = worker_python
        self.install_workers = install_workers
        self._runner = runner or self._default_runner
        # Where a run's agents come from: the built-ins, `[[agents]]`, then
        # the agents people saved (built per config, like the API's).
        self._agents: tuple[Config, AgentRegistry] | None = None
        # Where a planned assignment reads each agent's remembered
        # context. None (the default) builds the item's own memory service
        # at dispatch, the same one its run gets; a test may set it.
        self.memory: MemoryBlocks | None = None
        self._stop = threading.Event()
        # What ends the wait between ticks early: a stop, and `wake()`, the
        # signal that something queued work from outside the tick (the
        # concierge's `start_workload`, an API admission). Cleared just
        # before each tick, so a wake that lands mid-tick is never lost:
        # the next wait returns at once and that tick finds the row.
        self._wake = threading.Event()
        # An operator's `stop`: unlike a signal, it lets a landing the
        # daemon is completing finish before the process exits.
        self._graceful = False
        # An operator's `restart` (#969): the marker written for the next
        # process, kept here so the daemon entry knows to ask the supervisor
        # for the relaunch once run_forever returns.
        self._restart: dict[str, Any] | None = None
        self._landing_threads: list[threading.Thread] = []
        # Pause is a set of named holds (#534): an operator's `pause` and a
        # deploy's `pause --hold deploy-<id>` coexist, and each side releases
        # only its own. The daemon idles while any hold stands. The set
        # lives in the store (revision 0010) and survives a restart; this
        # is its write-through cache, loaded before anything can ask.
        self._holds_lock = threading.Lock()
        self._holds: set[str] = {h.name for h in self.dstore.holds()}
        # The runs in flight, oldest first (insertion order), keyed by run
        # id; at most `[daemon] max_concurrent_runs` of them. Changed only
        # on the loop thread (launch and reap) and under `_current_lock`;
        # controls on other threads read it under the lock.
        self._runs: dict[str, RunHandle] = {}
        self._current_lock = threading.Lock()
        # Every mutating control leaves a durable record here before it
        # acts; `recover()` stamps the generation that claims them.
        self.operations = OperationStore(dstore)
        self.generation: str | None = None
        # The item whose claim is in progress: `status()` reports it so a
        # restart is never timed into the window between the claim comment
        # landing on the source and the claim being persisted (#530).
        self._claiming: str | None = None
        # An operator cancel per run in flight, consumed when that run settles.
        self._cancel_requests: dict[str, CancelRequest] = {}
        # Breaker state lives in the store: a crash-restart loop must not
        # reset it (#254). These attributes are the write-through cache.
        self._breaker_opened_at, self._consecutive_failures = self.dstore.breaker()
        # Half-open (a breaker past its cooldown, or a provider hold past its
        # wait) lets one probe run through: the run id each probe became,
        # until it settles. While a probe is live nothing else launches.
        self._breaker_half_open = False
        self._breaker_probe: str | None = None
        self._provider_half_open = False
        self._provider_probe: str | None = None
        self._last_cap_log = 0.0
        self._last_idle_kind: str | None = None
        # Poll backoff: consecutive failures and the earliest next poll, so a
        # source that is down (GitHub outage, dead github sandbox) is not
        # hammered every tick.
        self._source_failures = 0
        self._source_next_poll = 0.0
        self._last_gc: float | None = None
        # Warm sandbox sets (#47): kept ready by a thread of their own when
        # `[daemon] warm_pairs` asks for them; a fresh dispatch takes one.
        self._warmer: Warmer | None = (
            Warmer(
                config,
                self.sbx,
                worker_python=self.worker_python,
                install_workers=self.install_workers is not False,
            )
            if config.daemon.warm_pairs > 0 and self.sbx is not None
            else None
        )
        self._warm_thread: threading.Thread | None = None
        # Schedules live in the store (#818); a `[[schedules]]` entry still
        # in sbxloop.toml is imported once, at first sight (the first tick
        # or schedule command, so its grid anchors where it always did),
        # and the operator is told the file's copy is now redundant.
        self._config_schedules_imported: list[str] | None = None
        # The import is reached from the loop thread (a tick) and from a
        # concierge command alike; one of them does it.
        self._schedules_lock = threading.Lock()
        # Where a repository is registered: the daemon's database. The
        # start-up that built the sources shares its registry (the file's
        # entries it imported are narrated once, at recovery); a loop built
        # on its own gets one of its own.
        self.repositories = repositories or RepositoryRegistry(config, dstore, clock=clock)
        # What this process polls: the enabled repositories at start, which
        # the sources were built from. A registration that changes this set
        # takes effect at the next start, and says so.
        self.polled_repos: frozenset[str] = frozenset()
        # The workspace budget pool: the daily run cap and token budget
        # every dispatch is admitted against, and what runs spend.
        self.usage_pool = UsagePool(dstore, lambda: self.config, clock)
        # `daemon_state` key: the day start the budget notice last went out for.
        self._budget_notice_key = "usage_pool_budget_notice_day"

    # -- external control ---------------------------------------------------------

    @property
    def current(self) -> RunHandle | None:
        """The oldest run in flight: what a bare ``cancel`` and a
        single-run reader mean by "the current run"."""
        with self._current_lock:
            return next(iter(self._runs.values()), None)

    @property
    def runs(self) -> list[RunHandle]:
        """Every run in flight, oldest first."""
        with self._current_lock:
            return list(self._runs.values())

    @property
    def _current(self) -> RunHandle | None:
        return self.current

    @_current.setter
    def _current(self, handle: RunHandle | None) -> None:
        # The single-run shape: the handle becomes the only run in flight.
        with self._current_lock:
            self._runs = {} if handle is None else {handle.run_id: handle}

    @property
    def _cancel_request(self) -> CancelRequest | None:
        """The oldest pending operator cancel, if any."""
        with self._current_lock:
            return next(iter(self._cancel_requests.values()), None)

    def _live_run(self, run_id: str) -> RunHandle | None:
        with self._current_lock:
            return self._runs.get(run_id)

    def _register(self, handle: RunHandle) -> None:
        with self._current_lock:
            self._runs[handle.run_id] = handle

    @property
    def _serial(self) -> bool:
        """One run at a time: a tick settles the run it dispatched before
        it returns, exactly as the loop always has."""
        return self.config.daemon.max_concurrent_runs <= 1

    def _run_repo(self, item: WorkItem) -> str | None:
        """The repository an item's run works on, the single-repo default
        standing in for an item that names none."""
        repo = self._item_repo(item)
        if repo is None:
            default = self.config.default_repo()
            repo = default.repo if default is not None else None
        return repo

    def _live_repos(self, *, code_only: bool = False) -> set[str]:
        repos: set[str] = set()
        for handle in self.runs:
            code = handle.item.kind == "code" and handle.item.recipe is None
            if code_only and not code:
                continue
            # A code run works the repository's checkout, named or not; any
            # other run only a repository it names.
            repo = self._run_repo(handle.item) if code else self._item_repo(handle.item)
            if repo is not None:
                repos.add(repo)
        return repos

    def _admission_blocked(self, item: WorkItem) -> str | None:
        """The repository that keeps ``item`` from starting beside the runs
        in flight, or ``None``. Two code runs never work one repository at
        once: they would race on its checkout, branches and pull requests."""
        if item.kind != "code" or item.recipe is not None:
            return None
        live = self._live_repos(code_only=True)
        if not live:
            return None
        repo = self._run_repo(item)
        return repo if repo is not None and repo in live else None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def paused(self) -> bool:
        with self._holds_lock:
            return bool(self._holds)

    @property
    def holds(self) -> list[str]:
        """The pause holds currently standing, sorted; empty when running."""
        with self._holds_lock:
            return sorted(self._holds)

    def pause(
        self,
        hold: str = OPERATOR_HOLD,
        *,
        by: str | None = None,
        via: str = "",
        reason: str = "",
        operation_id: str | None = None,
        owner_id: str | None = None,
    ) -> list[str]:
        """Take a named pause hold. Idempotent per name. Returns the holds
        standing afterwards. The hold is persisted before it is narrated
        and survives a restart; ``by``/``via`` say whose it is. The change
        is narrated once per transition — a deploy's hold and an operator's
        pause both show up in the chronology, so a paused daemon always
        says who is holding it. ``owner_id`` is the principal's stable id
        when the surface has one (``by`` is the display); a release that
        checks ownership compares against it."""
        hold = hold_name(hold)
        with self._holds_lock:
            fresh = self.dstore.take_hold(
                hold,
                self.clock(),
                owner_id=owner_id or by,
                owner_display=by,
                via=via,
                reason=reason,
                operation_id=operation_id,
            )
            self._holds.add(hold)
            holds = sorted(self._holds)
        if fresh:
            self._notice(
                "daemon.paused",
                f"paused by {hold}" + (f" ({by})" if by else "") + f"; holds: {', '.join(holds)}",
                hold=hold,
                by=by,
                holds=holds,
            )
        return holds

    def unpause(self, hold: str | None = OPERATOR_HOLD, *, by: str | None = None) -> list[str]:
        """Release one named hold (``None`` releases every hold — the
        operator's override for a hold whose owner died without releasing
        it). Returns the holds still standing; the daemon resumes claiming
        only when that is empty."""
        with self._holds_lock:
            if hold is not None:
                hold = hold_name(hold)
            released = self.dstore.release_hold(hold)
            if hold is None:
                self._holds.clear()
            else:
                self._holds.discard(hold)
            holds = sorted(self._holds)
        if released:
            self._notice(
                "daemon.resumed",
                f"hold released: {', '.join(released)}"
                + (f" ({by})" if by else "")
                + (f"; still paused by {', '.join(holds)}" if holds else "; claiming again"),
                released=released,
                by=by,
                holds=holds,
            )
        return holds

    def hold_details(self) -> list[dict[str, Any]]:
        """Every standing hold with its owner, for a status that says whose
        it is rather than just that one stands."""
        return [
            {
                "name": h.name,
                "owner": h.owner_display,
                "via": h.via,
                "reason": h.reason,
                "created_at": h.created_at,
            }
            for h in self.dstore.holds()
        ]

    def request_stop(self) -> None:
        """Operator ``stop``: claim nothing new, finish the run in flight
        and any landing in progress, then let ``run_forever`` return."""
        self._graceful = True
        self._stop.set()
        self._wake.set()

    # -- restart (#969) ------------------------------------------------------------

    def supervisor(self) -> str | None:
        """Who starts this daemon again once it exits: ``systemd`` when the
        process runs inside a unit (systemd stamps ``INVOCATION_ID`` on
        every process it starts), ``declared`` when ``[daemon] supervised``
        says an operator's own supervisor does, else ``None`` — a daemon
        started by hand, which a restart must never exit into nothing."""
        if os.environ.get("INVOCATION_ID"):
            return "systemd"
        if self.config.daemon.supervised:
            return "declared"
        return None

    @property
    def restart_pending(self) -> bool:
        return self._restart is not None

    def request_restart(
        self,
        *,
        by: str | None,
        reason: str,
        now: bool = False,
        **fields: Any,
    ) -> None:
        """Operator ``restart``: like ``request_stop`` — nothing new is
        claimed, the run and landing in flight finish (``now`` cancels the
        run first; it is resumable) — plus a marker in the store so the
        process that comes back can say why it did. ``fields`` ride the
        marker for that report (a config key and value, #971). Refused by
        name when no supervisor would start the daemon again."""
        supervisor = self.supervisor()
        if supervisor is None:
            raise ValueError(UNSUPERVISED_REFUSAL)
        who = by or "operator"
        marker = {
            "by": who,
            "reason": reason,
            "requested_at": self.clock(),
            "mode": "now" if now else "graceful",
            "supervisor": supervisor,
            **fields,
        }
        self.dstore.set_value(RESTART_MARKER_KEY, json.dumps(marker))
        self._restart = marker
        self._notice(
            "daemon.restart_requested",
            f"restart requested by {who}: {reason} — "
            + ("cancelling the current run first" if now else "after the current run"),
            by=who,
            reason=reason,
            mode=marker["mode"],
            supervisor=supervisor,
        )
        if now:
            for handle in self.runs:
                self._cancel_live(handle.run_id, who)
        self.request_stop()

    def _report_restart(self) -> None:
        """The first thing a started daemon says when the previous process
        left a restart marker: who asked and why. A marker older than the
        claim-staleness window is reported as stale, never as this start's
        cause — a restart that was requested and never completed is worth
        knowing about, but it is not what just happened."""
        raw = self.dstore.get_value(RESTART_MARKER_KEY)
        if raw is None:
            return
        self.dstore.set_value(RESTART_MARKER_KEY, None)
        try:
            marker = json.loads(raw)
        except ValueError:
            marker = None
        if not isinstance(marker, dict):
            log.warning("daemon.restart_marker_invalid", raw=raw[:200])
            return
        who = str(marker.get("by") or "operator")
        reason = str(marker.get("reason") or "operator restart")
        try:
            age = self.clock() - float(marker.get("requested_at", 0.0))
        except (TypeError, ValueError):
            age = float("inf")
        extra = {
            key: value
            for key, value in marker.items()
            if key not in ("by", "reason", "requested_at", "mode", "supervisor")
        }
        if age > self.config.daemon.claim_stale_after_s:
            self._notice(
                "daemon.restart_marker_stale",
                f"a restart requested by {who} ({reason}) never completed; this start is not it",
                level="warning",
                by=who,
                reason=reason,
                age_s=round(age, 1) if age != float("inf") else None,
            )
            return
        text = f"restarted by {who}: {reason} — up again {age:.0f}s after the request"
        key = marker.get("key")
        if isinstance(key, str) and key:
            # A config change (#971): the process that actually read the file
            # says whether the value is what it now sees, or which layer
            # still wins — the same judgement the write made, re-made here.
            text = f"restarted to apply {reason} (by {who}) — {self._config_in_effect(key)}"
        self._notice(
            "daemon.restarted",
            text,
            by=who,
            reason=reason,
            mode=marker.get("mode"),
            after_s=round(age, 1),
            **extra,
        )

    def _config_in_effect(self, key: str) -> str:
        from sbxloop.configedit import ConfigEditError, ConfigEditor
        from sbxloop.configedit.edit import FILE_LAYER

        try:
            row = ConfigEditor(self.config.paths, os.environ).describe(key)
        except (ConfigEditError, SbxloopError) as exc:
            return f"could not re-read `{key}`: {exc}"
        if row.source == FILE_LAYER:
            return "now in effect"
        if row.source == "unset":
            return f"`{key}` is not a setting this daemon knows"
        return f"written, but {row.source} sets `{row.display}` and wins"

    def cancel_current(
        self,
        requester: str | None = None,
        *,
        retry: bool = False,
        operation_id: str | None = None,
    ) -> bool:
        """Operator cancel of the in-flight run — the bare ``cancel``. The
        run is resolved under the current lock and cancelled by identity
        (:meth:`cancel_run`), so the request can never be attributed to a
        later run. ``False`` when nothing is running."""
        handle = self.current
        if handle is None:
            return False
        return self._cancel_live(handle.run_id, requester, retry=retry, operation_id=operation_id)

    def _cancel_live(
        self,
        run_id: str,
        requester: str | None,
        *,
        retry: bool = False,
        operation_id: str | None = None,
    ) -> bool:
        try:
            self.cancel_run(run_id, by=requester, retry=retry, operation_id=operation_id)
        except ControlError as exc:
            if exc.code in ("already_terminal", "not_eligible", "unknown_target"):
                # The run finished between the two looks: nothing is running.
                return False
            raise
        return True

    def cancel_run(
        self,
        run_id: str,
        *,
        by: str | None = None,
        retry: bool = False,
        expected_revision: int | None = None,
        operation_id: str | None = None,
    ) -> CancelOutcome:
        """Cancel one specific run, whatever state the daemon holds it in.

        The run in flight is asked to stop at its next boundary (the item
        settles as *cancelled* — no retry, no breaker count — unless
        ``retry`` asks for a fresh run); a run parked on a provider outage
        or pinned to a queued item awaiting resume is settled here, without
        touching a sandbox. Refused by name for a run that is terminal
        (``already_terminal``, with the state it reached), unknown, or not
        this daemon's to cancel. ``expected_revision`` is checked against
        the run row under the same lock as the request is recorded, so a
        cancel meant for an earlier state of the run is ``stale_revision``
        rather than applied. ``operation_id`` is the durable record the
        settle step finishes."""
        with self._current_lock:
            handle = self._runs.get(run_id)
            if handle is not None:
                self._check_revision(run_id, expected_revision)
                previous = self._cancel_requests.get(run_id)
                self._cancel_requests[run_id] = CancelRequest(
                    run_id, by or "operator", retry, operation_id
                )
        if handle is not None:
            if previous is not None and previous.operation_id is not None:
                # Superseded before it was honoured: the later request
                # carries the same effect, and the earlier record must not
                # claim it.
                self.operations.finish(
                    previous.operation_id,
                    self.clock(),
                    state="failed",
                    error_code="superseded",
                    error_detail=f"replaced by a later cancel of {run_id}",
                )
            handle.engine.request_cancel()
            return CancelOutcome(mode="current", retry=retry, target=run_id)
        self._check_revision(run_id, expected_revision)
        provider = self._provider_item(run_id)
        if provider is not None:
            message = self.cancel_provider(run_id, by)
            return CancelOutcome(mode="provider", target=run_id, message=message)
        try:
            record = self.store.get_run(run_id)
        except SbxloopError as exc:
            raise ControlError("unknown_target", f"unknown run {run_id}") from exc
        item_id = self.dstore.item_for_run(run_id)
        item = self.dstore.get(item_id) if item_id else None
        pinned = item is not None and item.run_id == run_id
        pending_resume = pinned and item is not None and item.state == "queued"
        if record.state in TERMINAL_RUN_STATES and not pending_resume:
            # Finished, not merely settled with a resume pending: the
            # honest answer names the state it reached.
            raise ControlError("already_terminal", f"run {run_id} is {record.state}")
        check_eligibility(
            "cancel",
            Subject(
                run_kind=record.kind,
                run_state=record.state,
                item_state=item.state if item else None,
                pinned=pinned,
            ),
        )
        if not pending_resume or item is None:
            raise ControlError(
                "not_eligible", f"run {run_id} is {record.state} but not in this daemon's hands"
            )
        # A pending resume: settling the run is what stops it, the same
        # way a provider-held run is settled.
        now = self.clock()
        reason = f"cancelled by {by or 'operator'} before its resume"
        self.dstore.mark_cancelled(item.item_id, reason, now)
        self.store.set_run_state(run_id, "cancelled")
        self.store.set_run_reason(run_id, reason)
        self.store.append_event(Event.now("run.cancelled", run_id, reason=reason))
        self._notice(
            "run.cancelled",
            f"⏹ {item.item_id} {reason}; `resume-run {run_id}` would continue it, "
            f"`retry {item.item_id}` reruns it fresh",
            item=item.item_id,
            run=run_id,
            by=by or "operator",
            requeued=False,
        )
        return CancelOutcome(mode="queued", target=run_id, message=reason)

    def steer_run(
        self,
        run_id: str,
        text: str,
        *,
        by: str | None = None,
        expected_revision: int | None = None,
        task_id: str | None = None,
        agent_slug: str | None = None,
    ) -> str:
        """Hand an instruction to the run in flight (#1038): the same
        ``post_user_message`` a chat thread uses, so the agent pauses at
        its next boundary, answers, and applies any course change.
        Returns the engine's message id, which the run's ``chat.reply``
        carries back. Refused by name when ``run_id`` is not the run in
        flight (``not_eligible`` with its state, or ``unknown_target``),
        when it is a tool run (nothing to steer), or when
        ``expected_revision`` is not the run's — all judged under the
        current-run lock, so the run cannot end between the check and the
        hand-over.

        ``task_id`` addresses one task lane, so the instruction is answered
        by that task rather than by whichever lane reaches a boundary first;
        ``agent_slug`` names the agent that was mentioned, and the answer
        comes back in its persona (S-A11). Neither is validated here: the
        engine holds the task board and the run's assignment, and falls back
        to run-level steering in its own default voice for a target it does
        not have."""
        with self._current_lock:
            handle = self._runs.get(run_id)
            if handle is not None:
                check_eligibility("steer", Subject(run_kind=handle.item.kind, is_current=True))
                self._check_revision(run_id, expected_revision)
                message_id = handle.engine.post_user_message(
                    text, task_id=task_id, agent_slug=agent_slug
                )
        if handle is None:
            try:
                record = self.store.get_run(run_id)
            except SbxloopError as exc:
                raise ControlError("unknown_target", f"unknown run {run_id}") from exc
            raise ControlError(
                "not_eligible",
                f"run {run_id} is {record.state}; steering needs the run in flight",
            )
        log.info(
            "run.steer",
            run=run_id,
            item=handle.item.item_id,
            by=by or "operator",
            message=message_id,
            chars=len(text),
            task=task_id,
            agent=agent_slug,
        )
        return message_id

    # -- mentions ------------------------------------------------------------------

    def _conversation_channels(self, handles: Sequence[RunHandle]) -> dict[str, str | None]:
        """Resolve live presentation after releasing the run lock.

        External work can acquire its conversation after dispatch. Looking
        up the durable association keeps controls current without rewriting
        the handle's admission, assignment, or scheduling fairness lane.
        """
        if not handles:
            return {}
        with self.dstore.read() as session:
            return {
                handle.run_id: channel_for_run(session, handle.run_id)
                or handle.item.channel_id
                or channel_for_item(session, handle.item.item_id)
                for handle in handles
            }

    def live_runs_for_agent(self, channel_id: str, agent_slug: str) -> list[MentionTarget]:
        """Every run in flight for ``channel_id`` that ``agent_slug`` works
        on, with the tasks bound to that agent which are still in flight.

        Read under the current-run lock so a run cannot finish between
        being listed and being steered.
        """
        targets: list[MentionTarget] = []
        with self._current_lock:
            handles = list(self._runs.values())
        channels = self._conversation_channels(handles)
        for handle in handles:
            if channels.get(handle.run_id) != channel_id:
                continue
            if not is_planned_assignment(handle.item.assignment_json):
                continue
            assert handle.item.assignment_json is not None
            try:
                assignment = AgentAssignment.from_json(handle.item.assignment_json)
            except ValueError:
                continue
            if agent_slug not in assignment.agents:
                continue
            # The admission snapshot names roles, not tasks: who does each
            # task is written by the engine once it has planned them, so
            # read it back from there, as the engine's own resume does.
            try:
                assignees = self.store.task_assignees(handle.run_id)
            except SbxloopError:
                assignees = {}
            tasks = assignment.with_tasks(assignees).tasks
            live = [
                task_id
                for task_id, slug in tasks.items()
                if slug == agent_slug and self._task_in_flight(handle.run_id, task_id)
            ]
            targets.append(MentionTarget(run_id=handle.run_id, task_ids=tuple(live)))
        return targets

    def _task_in_flight(self, run_id: str, task_id: str) -> bool:
        """Whether ``task_id`` is a task this run is still working on."""
        try:
            tasks = self.store.get_tasks(run_id)
        except SbxloopError:
            return False
        return any(task.spec.id == task_id and not task.terminal for task in tasks)

    def live_runs_in_channel(self, channel_id: str) -> list[str]:
        """Every run in flight that answers to ``channel_id``."""
        with self._current_lock:
            handles = list(self._runs.values())
        channels = self._conversation_channels(handles)
        return [handle.run_id for handle in handles if channels.get(handle.run_id) == channel_id]

    def stop_channel(
        self,
        channel_id: str,
        principal: Principal,
        *,
        agent_slug: str | None = None,
    ) -> list[str]:
        """Cancel the runs a channel's ``/stop`` means, and say which (S-A11).

        ``agent_slug`` narrows it to the runs that agent works on, which is
        what an exact ``@agent stop`` asks for. Every cancel goes through
        :class:`ControlService`, so a stop from chat is recorded as the same
        operation a stop from the API is -- there is one way to cancel a
        run, whichever surface asked.

        A run that refuses (it settled between being listed and being
        cancelled) is left out rather than failing the whole stop: the
        person asked for the channel to stop, not for a transaction.
        """
        from sbxloop.daemon.controls.service import ControlService, require

        # Refused as a whole, before anything is listed: a person who may
        # not cancel a run may not cancel a channel's worth of them.
        require(principal, "runs:control")
        if agent_slug is None:
            run_ids = self.live_runs_in_channel(channel_id)
        else:
            run_ids = [t.run_id for t in self.live_runs_for_agent(channel_id, agent_slug)]
        service = ControlService(self)
        stopped: list[str] = []
        for run_id in run_ids:
            try:
                service.cancel_run(principal, run_id)
            except ControlError as exc:
                log.info("channel.stop_refused", channel=channel_id, run=run_id, why=exc.message)
                continue
            stopped.append(run_id)
        log.info("channel.stopped", channel=channel_id, agent=agent_slug, runs=stopped)
        return stopped

    def route_mention(
        self,
        channel_id: str,
        agent_slug: str,
        text: str,
        principal: Principal,
    ) -> SteerOutcome | None:
        """Steer the run an ``@agent`` mention in ``channel_id`` is about
        (S-A11), or None when the mention is not about live work.

        One run, one target: when exactly one task in that run is bound to
        the agent and still in flight, the instruction goes to that task's
        lane; otherwise it goes to the run, which is where steering has
        always landed. Several live runs in one channel name the agent is
        the same ambiguity — there is no "the" run to steer — so the
        mention is left to be an ordinary turn.
        """
        targets = self.live_runs_for_agent(channel_id, agent_slug)
        if len(targets) != 1:
            return None
        target = targets[0]
        task_id = target.task_ids[0] if len(target.task_ids) == 1 else None
        from sbxloop.daemon.controls.service import ControlService

        return ControlService(self).steer(
            principal,
            target.run_id,
            text,
            task_id=task_id,
            agent_slug=agent_slug,
        )

    def _check_revision(self, run_id: str, expected: int | None) -> None:
        if expected is None:
            return
        try:
            current = self.store.get_run(run_id).revision
        except SbxloopError as exc:
            raise ControlError("unknown_target", f"unknown run {run_id}") from exc
        if current != expected:
            raise ControlError(
                "stale_revision",
                f"run {run_id} is at revision {current}, not {expected}",
                revision=current,
            )

    def resume_run(
        self,
        run_id: str,
        *,
        by: str | None = None,
        expected_revision: int | None = None,
    ) -> ResumeOutcome:
        """An operator's resume of a persisted run, admitted to this
        daemon's queue: the next tick resumes it through the breaker, the
        daily cap, the holds and the per-item resume budget, exactly as an
        interrupted run recovered at start is. Never a second engine — the
        CLI's ``sbxloop resume`` is for a run no daemon owns. Refused by
        name when the run is in flight, finished, unpinned, past its resume
        budget, exhausted, or its workspace was pruned."""
        if self._live_run(run_id) is not None:
            raise ControlError("not_eligible", f"run {run_id} is in flight")
        self._check_revision(run_id, expected_revision)
        try:
            record = self.store.get_run(run_id)
        except SbxloopError as exc:
            raise ControlError("unknown_target", f"unknown run {run_id}") from exc
        item_id = self.dstore.item_for_run(run_id)
        item = self.dstore.get(item_id) if item_id else None
        pinned = item is not None and item.run_id == run_id
        check_eligibility(
            "resume",
            Subject(
                run_kind=record.kind,
                run_state=record.state,
                item_state=item.state if item else None,
                pinned=pinned,
            ),
        )
        assert item is not None  # nosec B101 - eligibility refused the unpinned case
        if workspace_pruned(self.store, run_id):
            raise ControlError(
                "not_eligible",
                f"run {run_id}: its workspace was removed by gc; it cannot be resumed — "
                f"`retry {item.item_id}` starts a new run",
            )
        if record.exhausted is not None and record.granted_rounds == 0:
            raise ControlError(
                "not_eligible",
                f"run {run_id} exhausted its {record.exhausted} fix rounds; "
                f"`grant-rounds {run_id} N` resumes it with more",
            )
        resumes = self.dstore.resumes_for_item(item.item_id)
        budget = self.config.daemon.max_resumes_per_item
        if resumes >= budget:
            raise ControlError(
                "not_eligible",
                f"{item.item_id} spent its resume budget ({resumes} of {budget}, "
                f"`[daemon] max_resumes_per_item`); `retry {item.item_id}` runs it again "
                "from scratch",
            )
        now = self.clock()
        try:
            fresh = self.dstore.admit_resume(item.item_id, run_id, now)
        except KeyError as exc:
            raise ControlError("not_eligible", str(exc.args[0])) from exc
        who = by or "operator"
        self._notice(
            "run.resume_requested",
            f"{who} asked to resume {run_id}; {fresh.item_id} continues it at the next tick",
            item=fresh.item_id,
            run=run_id,
            by=who,
        )
        return ResumeOutcome(run_id=run_id, item_id=fresh.item_id)

    def _take_cancel(self, run_id: str) -> CancelRequest | None:
        """The cancel request for ``run_id``, consumed. Any other pending
        request whose run is no longer in flight is stale and dropped — and
        its record finished as such, so nobody reads a dropped cancel as
        honoured. Requests for the other runs in flight stay."""
        with self._current_lock:
            request = self._cancel_requests.pop(run_id, None)
            stale = [
                self._cancel_requests.pop(other)
                for other in list(self._cancel_requests)
                if other not in self._runs
            ]
        for dropped in stale:
            self._drop_cancel(dropped)
        return request

    def _drop_cancel(self, request: CancelRequest) -> None:
        if request.operation_id is not None:
            self.operations.finish(
                request.operation_id,
                self.clock(),
                state="failed",
                error_code="target_already_terminal",
                error_detail=f"run {request.run_id} was no longer in flight",
            )

    def _finish_cancel_record(self, cancel: CancelRequest, *, honoured: bool, run_id: str) -> None:
        """Settle the cancel's durable record from what the run did."""
        if cancel.operation_id is None:
            return
        if honoured:
            self.operations.finish(
                cancel.operation_id,
                self.clock(),
                state="succeeded",
                result={"mode": "current", "retry": cancel.retry, "run_id": run_id},
            )
            return
        state: str
        try:
            state = self.store.get_run(run_id).state
        except SbxloopError:
            state = "unknown"
        self.operations.finish(
            cancel.operation_id,
            self.clock(),
            state="failed",
            error_code="target_already_terminal",
            error_detail=f"the run finished before the cancel was honoured; it is {state}",
        )

    # -- operator item controls (#229) --------------------------------------------

    def abandon_item(self, item_id: str, reason: str | None = None) -> WorkItem:
        """Give up on an item deliberately. If it is the run in flight, the
        engine is asked to cancel and the settle path reports the abandon
        (so the source hears about it exactly once, after the run is really
        down); otherwise the source is told right here."""
        item_id = normalize_item_id(item_id)
        why = reason or "abandoned by operator"
        now = self.clock()
        before = self.dstore.get(item_id)
        fresh = self.dstore.abandon(item_id, why, now)
        if before is not None and before.state == "gated" and before.run_id is not None:
            gate = self.dstore.merge_gate_for(before.run_id)
            if gate is not None and gate.state in ("open", "approving"):
                self.dstore.resolve_merge_gate(before.run_id, "dismissed", why, now)
                self._frontend_gate_resolved(fresh, before.run_id, gate, "dismissed", None, why)
                self._notice(
                    "gate.dismissed",
                    f"🚪 {item_id}: held result dropped unpublished ({why})"
                    if gate.kind == "publish"
                    else f"🚪 {item_id}: merge gate dismissed ({why}); the PR stays open",
                    item=item_id,
                    run=before.run_id,
                )
        if before is not None and before.state in ("awaiting_review", "paused_review"):
            hold = self.dstore.review_hold_for(before.run_id or item_id)
            if hold is not None and hold.state in ("open", "approving", "paused", "fixing"):
                self.dstore.resolve_review_hold(hold.run_id, "dismissed", None, now, why)
                self._notice(
                    "review.dismissed",
                    f"🚪 {item_id}: no longer waiting for a review ({why}); the PR stays open",
                    item=item_id,
                    run=hold.run_id,
                )
        if self._cancel_if_current(item_id):
            self._notice(
                "item.abandon_cancelling",
                f"abandoning {item_id}: cancelling its run {fresh.run_id}",
                item=item_id,
                run=fresh.run_id,
                reason=why,
            )
            return fresh
        if fresh.run_id is not None:
            # A pinned run that is not the one in flight is dead (a pending
            # resume): its microVMs and secrets would otherwise outlive the
            # ledger row that recovery uses to find them.
            self._close_dead_run(fresh.run_id, "abandoned", now, repo=fresh.repo)
        self._deliver_report(fresh)
        return fresh

    def retry_item(self, item_id: str, by: str | None = None) -> WorkItem:
        """Put a settled (failed/blocked/cancelled) item back in the queue
        with a clean slate at a human's request — fresh plan, not a resume —
        and tell the source who did it (re-claim, drop the failed label)."""
        item_id = normalize_item_id(item_id)
        who = by or "operator"
        fresh = self.dstore.retry(item_id, self.clock(), f"re-queued by {who}")
        log.info("item.retry", item=item_id, by=who)
        self._deliver_report(fresh, by=who)
        return fresh

    def requeue_item(self, item_id: str) -> WorkItem:
        """Drop an item's pinned run so its next dispatch starts fresh
        (attempts intact). Cancels the run if it is the one in flight."""
        item_id = normalize_item_id(item_id)
        before = self.dstore.get(item_id)
        pinned = before.run_id if before is not None else None
        now = self.clock()
        fresh = self.dstore.requeue(item_id, now)
        if self._cancel_if_current(item_id):
            self._notice(
                "item.requeue_cancelling",
                f"requeue: {item_id} — cancelling its run {pinned}",
                item=item_id,
                run=pinned,
            )
        elif pinned is not None:
            self._close_dead_run(pinned, "requeued", now, repo=before.repo if before else None)
            self._notice(
                "item.requeue_unpinned",
                f"requeue: {item_id} unpinned from {pinned}",
                item=item_id,
                run=pinned,
            )
        else:
            self._notice("item.requeued", f"requeue: {item_id} re-queued", item=item_id)
        return fresh

    def _close_dead_run(
        self, run_id: str, result: str, now: float, *, repo: str | None = None
    ) -> None:
        """A pinned run that will never be resumed: drop its sandboxes and
        secrets first (so an interruption here leaves the ledger open for
        recovery to finish the job), then close its ledger row."""
        self._remove_stale_run_sandboxes(run_id, repo=repo)
        self._end_run_cancelled(run_id, f"cancelled: run {result} (never resumed)", source=result)
        self.dstore.finish_ledger(run_id, result, now)

    def _end_run_cancelled(
        self,
        run_id: str,
        reason: str,
        *,
        requester: str | None = None,
        retry: bool = False,
        source: str,
    ) -> None:
        """Transition the *engine* run record to the terminal state
        ``cancelled`` and append (never mutate) a chronology event saying so.

        Without this the run row is only ever written by the in-process run
        loop, so a cancelled item left a phantom ``running`` run behind
        (#374). A run already in a terminal state, or with no record at all
        (died before ``create_run``), is a no-op.
        """
        try:
            record = self.store.get_run(run_id)
        except (SbxloopError, StateError):
            log.debug("run.cancel_no_record", run=run_id, hint="died before create_run")
            return
        if record.state in TERMINAL_RUN_STATES:
            return
        try:
            self.store.reconcile_run(run_id, "cancelled", reason)
            self.store.append_event(
                Event.now(
                    "run.cancelled",
                    run_id,
                    reason=reason,
                    by=requester,
                    requeued=retry,
                    via=source,
                )
            )
        except (SbxloopError, StateError) as exc:
            log.warning("run.cancel_write_failed", run=run_id, error=str(exc))

    def _deliver_pending_reports(self) -> None:
        """Tell the source about decisions it has not heard: an operator's
        ``sbxloop daemon abandon|retry`` from another process can only flip
        the row, and a run's merged/blocked report may have failed on a
        GitHub hiccup. Runs at the top of every tick and after recovery; the
        in-flight item is not here (its settle path delivers, once the run
        is really down)."""
        for item in self.dstore.pending_reports():
            self._deliver_report(item)

    def _deliver_report(self, item: WorkItem, *, by: str | None = None) -> None:
        """Deliver ``item.pending_report`` to the source, exactly once.

        Operator decisions (abandon/requeue) take the debt atomically first,
        so a Discord command on the bridge thread and the tick sweep on the
        loop thread cannot both report. A run outcome (merged/blocked) takes
        it only once the source confirms every step landed — an issue close
        that did not happen must be retried, not recorded.
        """
        kind = item.pending_report
        if kind is None:
            return
        if kind in ("merged", "blocked", "gated", "completed", "held"):
            if self._report_outcome(item, kind):
                self.dstore.take_pending_report(item.item_id)
            return
        if not self.dstore.take_pending_report(item.item_id):
            return
        if kind == "abandoned":
            why = item.last_error or "abandoned by operator"
            self.source.report_abandoned(item, why)
            self._notice(
                "item.abandoned",
                f"❌ {item.item_id} abandoned by operator: {why}",
                item=item.item_id,
                run=item.run_id,
                reason=why,
            )
        else:
            # A row-only retry records who asked as its last_error.
            who = by or (item.last_error or "").removeprefix("re-queued by ") or "operator"
            self.source.report_requeued(item, who)
            self._notice(
                "item.requeued",
                f"↻ {item.item_id} re-queued by {who} (attempts reset)",
                item=item.item_id,
                by=who,
            )

    def _report_outcome(self, item: WorkItem, kind: str) -> bool:
        """Pay a merged/blocked/completed/held report; True when the source
        confirmed it."""
        if kind == "held":
            delivered = self.source.report_held(item)
            if not delivered:
                log.warning("report.deferred", item=item.item_id, kind=kind)
            return delivered
        if kind == "completed":
            # A workload's result (#760): the source gets the report the
            # finish card was built from, not a pull request.
            if item.run_id is None:
                return True
            try:
                report = self.report_for(item.run_id)
            except (SbxloopError, StateError):
                log.debug("report.no_run_record", item=item.item_id, run=item.run_id)
                return True
            delivered = self.source.report_completed(item, report)
            if not delivered:
                log.warning(
                    "report.deferred",
                    item=item.item_id,
                    kind=kind,
                    hint="the source did not confirm; retried on the next tick",
                )
            return delivered
        pr_number: int | None = None
        pr_url = ""
        if item.run_id is not None:
            try:
                record = self.store.get_run(item.run_id)
                pr_number, pr_url = record.pr_number, record.pr_url or ""
            except (SbxloopError, StateError):
                log.debug("report.no_run_record", item=item.item_id, run=item.run_id)
        if kind == "merged":
            delivered = self.source.report_merged(item, pr_number, pr_url)
        elif kind == "gated":
            delivered = self.source.report_gated(item, pr_number, pr_url)
        else:
            reason = item.last_error or "GitHub would not let the loop finish the pull request"
            delivered = self.source.report_blocked(item, reason, pr_number, pr_url)
        if not delivered:
            log.warning(
                "report.deferred",
                item=item.item_id,
                kind=kind,
                hint="the source did not confirm; retried on the next tick",
            )
        return delivered

    def _cancel_if_current(self, item_id: str) -> bool:
        wanted = normalize_item_id(item_id)
        handles = [h for h in self.runs if h.item.item_id == wanted]
        for handle in handles:
            handle.engine.request_cancel()
        return bool(handles)

    def _operator_override(self, item_id: str, run_id: str) -> WorkItem | None:
        """The item row is the daemon's only channel from a CLI running in
        another process: an item that is no longer ``running`` on *this*
        run was abandoned/requeued by an operator. Returns the fresh row
        when so."""
        fresh = self.dstore.get(normalize_item_id(item_id))
        if fresh is None or (fresh.state == "running" and fresh.run_id == run_id):
            return None
        return fresh

    def quiesce(self) -> None:
        """The cleanup registry's shutdown hook: stop claiming, ask the
        in-flight engine to cancel, wait briefly. Stable across runs — it
        looks up the current engine at call time."""
        self._stop.set()
        self._wake.set()
        handles = self.runs
        if not handles:
            log.info("daemon.quiesce", run=None)
        for handle in handles:
            log.info(
                "daemon.quiesce",
                item=handle.item.item_id,
                run=handle.run_id,
                grace_s=self.config.daemon.shutdown_grace_s,
            )
            handle.engine.request_cancel()
        # One grace period for all of them, not one each.
        deadline = time.monotonic() + self.config.daemon.shutdown_grace_s
        for handle in handles:
            thread = handle.thread
            if thread is None or not thread.is_alive():
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                log.warning(
                    "daemon.quiesce_timeout",
                    run=handle.run_id,
                    grace_s=self.config.daemon.shutdown_grace_s,
                    hint="engine still running past shutdown grace; the run stays resumable",
                )

    def _provider_recovery(self) -> ProviderRecovery:
        return ProviderRecovery(self.store, self.config.agent.backend, clock=self.clock)

    def _release_request_rejection_hold(self) -> None:
        """At startup, release a provider hold recorded for a refused request
        (:meth:`ProviderRecovery.release_request_rejection`), saying so. Never
        fatal: a daemon that cannot read its holds still starts, and the hold
        stays for ``resume <backend>``."""
        try:
            released = self._provider_recovery().release_request_rejection()
        except Exception:
            log.warning("provider.hold_release_failed", exc_info=True)
            return
        if released is None:
            return
        failure = released.failure
        self._notice(
            "provider.hold_released",
            f"released the {failure.backend} provider hold recorded for a request the endpoint "
            f"refused (HTTP {failure.http_status}): waiting cannot fix a refused request, so the "
            f"next call is judged by this release. It was: {failure.reason}",
            level="warning",
            backend=failure.backend,
            http_status=failure.http_status,
        )

    def _provider_item(self, target: str) -> WorkItem | None:
        for item in self.dstore.items():
            if (
                item.run_id is not None
                and (target == item.run_id or normalize_item_id(target) == item.item_id)
                and item.state in ("queued", "cancelled")
                and self._provider_recovery().pending(item.run_id)
            ):
                return item
        return None

    def cancel_provider(self, target: str, by: str | None = None) -> str:
        item = self._provider_item(target)
        if item is None or item.run_id is None:
            raise ValueError(f"{target!r} has no parked provider run")
        reason = f"cancelled by {by or 'operator'} while waiting for the provider"
        self.dstore.mark_cancelled(item.item_id, reason, self.clock())
        self.store.set_run_state(item.run_id, "cancelled")
        self.store.set_run_reason(item.run_id, reason)
        self.store.append_event(Event.now("run.cancelled", item.run_id, reason=reason))
        return f"{item.item_id}: cancelled; checkpoint retained for resume"

    def status(self) -> dict[str, Any]:
        now = self.clock()
        day_start, day_end = day_window(now, self.config.daemon.run_cap_timezone)
        handles = self.runs
        provider_hold = self._provider_recovery().hold()
        return {
            "provider_hold": provider_hold.summary() if provider_hold else None,
            # The oldest run in flight, as a single-run reader expects it.
            "current": handles[0].snapshot() if handles else None,
            # Every run in flight, oldest first.
            "runs": [{**h.snapshot(), "repo": h.item.repo} for h in handles],
            "max_concurrent_runs": self.config.daemon.max_concurrent_runs,
            "warm": (
                {"ready": len(self._warmer.ready()), "target": self._warmer.target}
                if self._warmer is not None
                else None
            ),
            "queued": len(self.dstore.queued()),
            "runs_today": self.dstore.runs_started_since(day_start),
            "runs_today_resets_at": day_end,
            "run_cap_timezone": self.config.daemon.run_cap_timezone,
            "resumes_today": self.dstore.resumes_since(day_start),
            "max_runs_per_day": self.config.daemon.max_runs_per_day,
            "breaker_open": self._breaker_open(now),
            "consecutive_failures": self._consecutive_failures,
            "paused": self.paused,
            "holds": self.holds,
            "hold_details": self.hold_details(),
            # The claim in progress, if any: not yet a run, but not idle
            # either — a restart here orphans the issue (#530).
            "claiming": self._claiming,
            "stopping": self._stop.is_set(),
            "restarting": self._restart is not None,
            # The process behind the answer: what a console needs to signal
            # it when no service manager stands in front, and to show uptime.
            "pid": os.getpid(),
            "started_at": self.started_at,
            "generation": self.generation,
            "version": __version__,
            # Where it loaded its configuration from: the console anchors
            # its editor there rather than on its own working directory.
            "cwd": str(Path.cwd()),
            # Per-repository polling health (#516); [] for a single-repo daemon.
            "repos": [
                {**h.to_json(), "state": h.state}
                for h in getattr(self.source, "repo_health", None) or []
            ],
            # Where work comes from (#760): "github", "chat", or both.
            "source": self.source.name,
            # Polling has its own backoff, independent of failed runs. A
            # daemon that cannot discover work must not appear healthy
            # merely because it has no failed runs or queued items.
            "source_failures": self._source_failures,
            "source_retry_in_s": max(0.0, self._source_next_poll - now),
        }

    # -- multi-repo polling health (#516) -----------------------------------------

    def source_notice(self, kind: str, repo: str, text: str) -> None:
        """The source's own transitions (suspended / recovered), narrated
        like any daemon notice so Discord sees a repository go dark once."""
        level: NoticeLevel = "error" if kind == "source.repo_suspended" else "info"
        self._notice(cast(NoticeKind, kind), text, level=level, repo=repo)

    def resume_repo(self, repo: str, by: str | None = None) -> dict[str, Any]:
        """Operator: poll a suspended (or backing-off) repository again now.
        Refused with the reason when the source has no such state."""
        resume = getattr(self.source, "resume_repo", None)
        if resume is None:
            raise ValueError("only a multi-repository daemon has per-repository polling state")
        health = resume(repo)
        who = by or "operator"
        self._notice(
            "source.repo_resumed",
            f"{who} resumed polling of {health.repo}",
            repo=health.repo,
            by=who,
        )
        return {**health.to_json(), "state": health.state}

    # -- main loop --------------------------------------------------------------------

    def run_forever(self) -> None:
        self._notice(
            "daemon.started",
            "daemon started",
            poll_interval_s=self.config.daemon.poll_interval_s,
            source=self.source.name,
        )
        self._release_request_rejection_hold()
        self._report_restart()
        # After the restart's own line, so a restart still reads as
        # "started, restarted by ..." and the import follows it.
        self.narrate_repository_import()
        self._start_warmer()
        ticks = 0
        try:
            while not self._stop.is_set():
                self._wake.clear()
                started = time.monotonic()
                result = self.tick()
                ticks += 1
                self._log_tick(result, time.monotonic() - started)
                # One run at a time, a tick that ran an item goes straight on
                # to the next. With room for more, the tick already filled
                # every slot it could: wait for a run to finish (or the poll).
                if result.dispatched is None or not self._serial:
                    self._idle_wait(self.config.daemon.poll_interval_s)
            # Nothing new is claimed; the runs in flight finish (or, after a
            # quiesce, stop at their boundary) and are settled here.
            self.drain()
            if self._graceful:
                self._join_landings()
        finally:
            self._notice("daemon.stopped", "daemon stopped", ticks=ticks)

    def _idle_wait(self, timeout: float) -> None:
        """Wait for the next poll. With runs in flight the wait ends as soon
        as one of them finishes, so its slot is refilled (and its item
        settled) without sitting out the poll interval, and an operator
        override from another process is noticed within a second. A
        ``wake()`` (work queued from outside the tick) ends it at once."""
        if not self.runs:
            self._wake.wait(timeout)
            return
        deadline = time.monotonic() + timeout
        while not self._stop.is_set():
            handles = self.runs
            if any(h.finished for h in handles):
                return
            self._poll_overrides(handles)
            left = deadline - time.monotonic()
            if left <= 0:
                return
            if self._wake.wait(min(1.0, left)):
                return

    def wake(self) -> None:
        """Something queued work from outside the tick: end the wait between
        ticks now rather than at the next poll interval. Cheap and
        idempotent; a wake with nothing to run costs one empty tick."""
        self._wake.set()

    # -- warm sandbox sets (#47) ------------------------------------------------

    def _start_warmer(self) -> None:
        warmer = self._warmer
        if warmer is None or self._warm_thread is not None:
            return
        self._warm_thread = threading.Thread(
            target=warmer.run_forever,
            args=(self._stop,),
            kwargs={"paused": lambda: self.paused, "run_finished": self._warm_run_finished},
            name="sbxloop-warmer",
            daemon=True,
        )
        self._warm_thread.start()
        log.info("warm.started", target=warmer.target, ttl_s=warmer.ttl_s)

    def _claim_warm(self, item: WorkItem) -> str | None:
        """A warm set's run id for a fresh run of ``item``, or None: no pool,
        nothing ready, or a tool run (its recipe stages the workspace the
        sandbox must mount, which no warm set has)."""
        if self._warmer is None or item.recipe is not None or item.kind == "tool":
            return None
        try:
            return self._warmer.claim()
        except Exception:
            log.warning("warm.claim_failed", item=item.item_id, exc_info=True)
            return None

    def _warm_run(self, run_id: str) -> bool:
        """Whether ``run_id`` was taken from the warm pool."""
        return self._warmer is not None and self._warmer.is_claimed(run_id)

    def _warm_run_finished(self, run_id: str) -> bool | None:
        """For the warmer's sweep: whether a claimed set's run has ended;
        None when no run of that id exists (yet)."""
        try:
            run = self.store.get_run(run_id)
        except StateError:
            return None
        return run.state in TERMINAL_RUN_STATES

    def drain(self) -> tuple[tuple[str, TickOutcome], ...]:
        """Wait for every run in flight to end and settle each one; what
        ``run_forever`` does once it stops claiming, and what a one-shot
        tick does before the process exits. Nothing when no run is live."""
        settled: list[tuple[str, TickOutcome]] = []
        while True:
            handles = [h for h in self.runs if h.thread is not None]
            if not handles:
                return tuple(settled)
            pending = [h for h in handles if not h.finished]
            if pending:
                self._poll_overrides(pending)
                thread = pending[0].thread
                assert thread is not None  # nosec B101 - filtered above
                thread.join(timeout=1.0)
            settled.extend(self._reap())

    def _log_tick(self, result: TickResult, duration_s: float) -> None:
        """Every tick at DEBUG; a *change* of why the daemon is idle at INFO,
        so a journal at the default level says once that the daemon is
        paused / breaker-open / backing off / capped — and once when work
        resumes — instead of nothing at all (or the same line each poll)."""
        log.debug(
            "daemon.tick",
            discovered=result.discovered,
            dispatched=result.dispatched,
            outcome=result.outcome,
            idle=result.idle_kind,
            idle_detail=result.idle_detail,
            duration_s=round(duration_s, 3),
        )
        kind = result.idle_kind if result.dispatched is None else None
        if kind != self._last_idle_kind:
            if kind is None:
                log.info("daemon.active", item=result.dispatched, outcome=result.outcome)
            else:
                log.info(
                    "daemon.idle",
                    idle=kind,
                    idle_detail=result.idle_detail,
                    queued=len(self.dstore.queued()),
                )
            self._last_idle_kind = kind

    def tick(self) -> TickResult:
        # Runs that finished since the last tick settle first, on this
        # thread: their outcome (a failure, a gate) is what the gates below
        # judge, and their slot is free for the dispatch that follows.
        settled = self._reap()
        result = self._tick()
        return result._replace(settled=settled) if settled else result

    def _tick(self) -> TickResult:
        now = self.clock()
        # Before the gates: a decision made from another process — or a
        # merged/blocked report GitHub refused last time — reaches the source
        # even while paused or with the breaker open.
        self._deliver_pending_reports()
        self._maybe_gc(now)
        # Liveness safety net for phantom active runs (#374); sweeps even while
        # paused, the very state the field report was filed from.
        self._reconcile_stale_runs(now)
        # A parked PR's review poll (#675) is not new work: it runs even
        # paused, so an approval given during a pause still lands.
        self._review_tick(now)
        idle = self._dispatch_gate(now, first=True)
        if idle is not None:
            return idle
        # What is already queued (a chat ask, an API admission, a schedule's
        # tick, an issue an earlier poll found) runs before the forge is
        # polled: the poll is seconds of sandbox execs, and a person's ask
        # does not wait behind it (field: 17-137s from ask to dispatch).
        # Schedules are the store's own clock, cheap and due on every tick,
        # so they fire first and their items join the same pass.
        fired = self._fire_schedules(now)
        result = self._dispatch_pass(now, discovered=fired)
        if result.launched and self._serial:
            # One item per tick, settled before the tick returns; the next
            # tick follows at once and polls then.
            return result
        discovered = fired + self._discover(now)
        if discovered == fired:
            return result
        if result.launched and (self._stop.is_set() or self._dispatch_gate(now) is not None):
            return result._replace(discovered=discovered)
        again = self._dispatch_pass(now, discovered)
        if not result.launched:
            return again
        if not again.launched:
            return result._replace(discovered=discovered)
        return result._replace(discovered=discovered, launched=result.launched + again.launched)

    def _dispatch_pass(self, now: float, discovered: int) -> TickResult:
        """Start queued items while there is room for them (one, when runs
        are serial); the tick's result when nothing more can start."""
        limit = self.config.daemon.max_concurrent_runs
        launched: list[str] = []
        outcome: TickOutcome | None = None
        while True:
            if launched and (self._stop.is_set() or self._dispatch_gate(now) is not None):
                break
            live = len(self.runs)
            if live >= limit:
                if launched:
                    break
                return TickResult(
                    discovered=discovered,
                    idle_kind="busy",
                    idle_detail=f"{live} of {limit} runs in flight",
                )
            waits: dict[str, str] = {}

            def blocked(candidate: WorkItem, waits: dict[str, str] = waits) -> bool:
                repo = self._admission_blocked(candidate)
                if repo is not None:
                    waits[candidate.item_id] = repo
                return repo is not None

            item = self.dstore.next_queued(
                now,
                self.config.daemon.retry_backoff_s,
                skip=blocked,
                busy=self._fairness_busy(),
            )
            if item is None:
                if launched:
                    break
                return self._nothing_to_run(now, discovered, waits)
            dispatched, item_outcome = self._start_item(item, now)
            launched.append(dispatched)
            self._bind_probes(dispatched)
            if outcome is None:
                outcome = item_outcome
            if self._serial:
                # One item per tick, settled before the tick returns.
                break
        return TickResult(
            discovered=discovered,
            dispatched=launched[0],
            outcome=outcome,
            launched=tuple(launched),
        )

    def _dispatch_gate(self, now: float, *, first: bool = False) -> TickResult | None:
        """Why nothing new may start right now, or ``None``. ``first`` is
        the tick's own look, which narrates the daily cap; a look between
        two launches only stops the loop."""
        if self.paused:
            return TickResult(idle_kind="paused")
        provider_hold = self._provider_recovery().hold()
        if provider_hold is not None and provider_hold.blocked(now):
            return TickResult(idle_kind="provider_held", idle_detail=provider_hold.summary())
        # A hold past its wait stays active until a call succeeds: one probe.
        self._provider_half_open = provider_hold is not None
        self._provider_probe = self._live_probe(self._provider_probe)
        if provider_hold is None:
            self._provider_probe = None
        elif self._provider_probe is not None:
            return TickResult(
                idle_kind="provider_held",
                idle_detail=(
                    f"{provider_hold.summary()}; probe run {self._provider_probe} in flight"
                ),
            )
        if self._breaker_open(now):
            return TickResult(idle_kind="breaker")
        self._breaker_probe = self._live_probe(self._breaker_probe)
        if self._breaker_half_open and self._breaker_probe is not None:
            return TickResult(
                idle_kind="breaker",
                idle_detail=f"half-open; probe run {self._breaker_probe} in flight",
            )
        admission = self.usage_pool.admit_run(None, now)
        if admission.ok:
            return None
        if admission.reason == "token_budget":
            if first:
                self._announce_budget(now)
            return TickResult(idle_kind="budget")
        day_start, day_end = day_window(now, self.config.daemon.run_cap_timezone)
        started_today = self.dstore.runs_started_since(day_start)
        if not first:
            return TickResult(idle_kind="daily_cap")
        provider_resume = any(
            item.run_id and self._provider_recovery().pending(item.run_id)
            for item in self.dstore.queued()
        )
        if provider_resume:
            # The run cap exempts a provider-held resume; the token budget
            # does not, even though the pool reported the run cap first.
            if not self.usage_pool.admit_tokens(now).ok:
                self._announce_budget(now)
                return TickResult(idle_kind="budget")
            return None
        if now - self._last_cap_log > 3600:
            self._last_cap_log = now
            cap = self.config.daemon.max_runs_per_day
            tz = self.config.daemon.run_cap_timezone
            self._notice(
                "daemon.daily_cap",
                f"run cap reached for today ({tz}): {started_today}/{cap}; resets at 00:00 {tz}",
                started_today=started_today,
                cap=cap,
                timezone=tz,
                resets_at=day_end,
            )
        return TickResult(idle_kind="daily_cap")

    def _fairness_busy(self) -> Callable[[WorkItem], bool] | None:
        """With room for several runs, an item whose requester (its
        :func:`fairness_key`) already has a run in flight waits behind one
        whose requester has none. One run at a time: plain FIFO."""
        if self._serial:
            return None
        live = {fairness_key(handle.item) for handle in self.runs}
        if not live:
            return None
        return lambda candidate: fairness_key(candidate) in live

    def _announce_budget(self, now: float) -> None:
        """Say once per pool day that the token budget holds new runs back.
        The day is remembered in the store, so a restart does not repeat it."""
        day_start, day_end = day_window(now, self.config.daemon.run_cap_timezone)
        stamp = repr(day_start)
        if self.dstore.get_value(self._budget_notice_key) == stamp:
            return
        self.dstore.set_value(self._budget_notice_key, stamp)
        budget = self.config.daemon.daily_token_budget
        spent = self.usage_pool.tokens_today(now)
        tz = self.config.daemon.run_cap_timezone
        self._notice(
            "daemon.token_budget",
            f"token budget reached for today ({tz}): {spent}/{budget} tokens; "
            f"no new runs until 00:00 {tz}",
            tokens_today=spent,
            budget=budget,
            timezone=tz,
            resets_at=day_end,
        )

    def _nothing_to_run(self, now: float, discovered: int, waits: dict[str, str]) -> TickResult:
        """Say WHY there is nothing to run: a queue full of items sitting in
        retry backoff reads as "no work" otherwise (field: --once after a
        failed attempt printed no_work with no explanation)."""
        if waits:
            item_id, repo = next(iter(waits.items()))
            more = f" (+{len(waits) - 1} more)" if len(waits) > 1 else ""
            return TickResult(
                discovered=discovered,
                idle_kind="busy",
                idle_detail=f"{item_id}{more} waits for the run in flight on {repo}",
            )
        waiting = self.dstore.queued()
        if waiting:
            soonest = min(
                max(
                    0.0,
                    w.attempts * self.config.daemon.retry_backoff_s - (now - w.updated_at),
                    (w.not_before - now) if w.not_before is not None else 0.0,
                )
                for w in waiting
            )
            return TickResult(
                discovered=discovered,
                idle_kind="backoff",
                idle_detail=f"{len(waiting)} queued; next eligible in {soonest:.0f}s",
            )
        return TickResult(discovered=discovered, idle_kind="no_work")

    def _start_item(self, item: WorkItem, now: float) -> tuple[str, TickOutcome]:
        """Claim ``item`` if it is not yet ours, then dispatch it (fresh or
        resuming its pinned run). The outcome is ``started`` for a run left
        executing, or how the item settled when it settled right here."""
        if not item.claimed:
            # The claim's token is persisted before the comment goes up, and
            # SIGTERM/SIGINT are held until the claim is complete (#530): a
            # process that dies mid-claim either never posted, or left a row
            # recovery can settle against the comment it did post.
            token = item.claim_token or uuid.uuid4().hex
            self.dstore.mark_claiming(item.item_id, token, now)
            item = self.dstore.get(item.item_id) or item.model_copy(update={"claim_token": token})
            self._claiming = item.item_id
            try:
                with defer_signals():
                    claimed = self.source.claim(item)
                    if claimed:
                        self.dstore.mark_claimed(item.item_id, now)
            finally:
                self._claiming = None
            if not claimed:
                # Not ours to run — another daemon won, the issue closed, the
                # trigger label went away, or GitHub was down. Never a
                # terminal row: that is what discovery dedups against, and
                # what made a lost race permanent. The next poll re-creates
                # the row if the trigger label is (still, or again) there.
                self.dstore.discard(item.item_id)
                self._notice(
                    "item.claim_failed",
                    f"could not claim {item.item_id} ({item.title}); forgotten — "
                    "re-queued by the next poll if the trigger label is still on it",
                    item=item.item_id,
                    title=item.title,
                )
                return item.item_id, "failed"
            log.info("item.claimed", item=item.item_id, title=item.title)
        if item.run_id is not None:
            return item.item_id, self._resume(item, now)
        return item.item_id, self._dispatch(item, resume_run_id=None)

    def _resume(self, item: WorkItem, now: float) -> TickOutcome:
        """Resume the run recovery pinned on a queued item — or, past the
        per-item resume budget, settle that run as a failed attempt so a
        plan that keeps getting interrupted cannot burn engine wall clock
        forever (#234)."""
        run_id = item.run_id
        assert run_id is not None
        if self._provider_recovery().pending(run_id):
            # Provider downtime neither spends the crash-resume budget nor
            # destroys the VM containing the interrupted SDK session.
            return self._dispatch(item, resume_run_id=run_id)
        gate = self.dstore.merge_gate_for(run_id)
        if gate is not None and gate.kind == "publish" and gate.state == "approving":
            # Not an interruption (#760): a held result was released and
            # the run resumes at its publishing stage. One stage, then done.
            self._remove_stale_run_sandboxes(run_id, repo=item.repo)
            self._notice(
                "run.resuming",
                f"resuming {run_id} for {item.item_id} at publishing — released by "
                f"{gate.resolved_by or 'an operator'}",
                item=item.item_id,
                run=run_id,
            )
            return self._dispatch(item, resume_run_id=run_id)
        hold = self.dstore.review_hold_for(run_id)
        if hold is not None and hold.state == "fixing":
            # Not an interruption either (#675): a reviewer asked for
            # changes on the parked PR and the run resumes at its landing
            # stage to make them. Bounded by the run's own fix rounds.
            self._remove_stale_run_sandboxes(run_id, repo=item.repo)
            self._notice(
                "run.review_resumed",
                f"🔁 resuming {run_id} for {item.item_id} on its own PR #{hold.pr_number} — "
                "a reviewer requested changes",
                item=item.item_id,
                run=run_id,
                url=hold.pr_url or None,
                pr=hold.pr_number,
            )
            return self._dispatch(item, resume_run_id=run_id)
        granted = self._granted_retry(run_id)
        if granted is not None:
            # Not an interruption (#523): the run ended one fix round short
            # and was granted more. The resume budget bounds crash-resume
            # churn; this continuation is bounded by the grant itself.
            self._remove_stale_run_sandboxes(run_id, repo=item.repo)
            self._notice(
                "run.resuming",
                f"resuming {run_id} for {item.item_id} on its own PR with {granted.granted_rounds} "
                f"granted fix round(s)",
                item=item.item_id,
                run=run_id,
                url=granted.pr_url,
                granted_rounds=granted.granted_rounds,
                pr=granted.pr_number,
            )
            return self._dispatch(item, resume_run_id=run_id)
        resumes = self.dstore.resumes_for_item(item.item_id)
        budget = self.config.daemon.max_resumes_per_item
        if resumes >= budget:
            self._notice(
                "run.resume_budget_exhausted",
                f"{item.item_id}: {run_id} interrupted again after {resumes} resume(s) "
                f"(budget {budget}); settling as a failed attempt",
                item=item.item_id,
                run=run_id,
                resumes=resumes,
                budget=budget,
            )
            return self._settle(
                item,
                run_id,
                None,
                StateError(f"run {run_id} interrupted; resume budget ({budget}) exhausted"),
            )
        # A dead process leaves its microVMs alive; resume re-provisions
        # under the same names and `sbx create` refuses a name that exists
        # (field: SIGKILL mid-run → 'sandbox already exists' on the very
        # next start).
        self._remove_stale_run_sandboxes(run_id, repo=item.repo)
        self._notice(
            "run.resuming",
            f"resuming {run_id} for {item.item_id} (resume {resumes + 1}/{budget})",
            item=item.item_id,
            run=run_id,
            resume=resumes + 1,
            budget=budget,
        )
        return self._dispatch(item, resume_run_id=run_id)

    def _granted_retry(self, run_id: str) -> RunRecord | None:
        """The run record when the pinned run is a *granted* continuation: it
        ended ``failed`` by exhausting a fix-round budget and has since been
        granted more rounds (by :meth:`_settle` or an operator)."""
        try:
            record = self.store.get_run(run_id)
        except SbxloopError:
            return None
        if record.state == "failed" and record.granted_rounds > 0 and record.exhausted is None:
            return record
        return None

    # -- operator: more rounds for an exhausted run (#523) ---------------------------

    def grant_rounds(self, run_id: str, rounds: int, by: str | None = None) -> WorkItem:
        """Give an exhausted run ``rounds`` more fix rounds and resume it now,
        skipping the retry backoff. The run must have ended by exhausting a
        budget and be pinned to an item that is failed (handed over) or
        queued (waiting out its backoff); anything else is refused with the
        state that is actually there."""
        if rounds < 1:
            raise ValueError(f"rounds must be at least 1, not {rounds}")
        try:
            record = self.store.get_run(run_id)
        except SbxloopError as exc:
            raise ValueError(f"unknown run {run_id}") from exc
        item_id = self.dstore.item_for_run(run_id)
        item = self.dstore.get(item_id) if item_id else None
        if item is None or item.run_id != run_id:
            raise ValueError(f"run {run_id} is not the pinned run of any work item")
        if record.state != "failed" or (record.exhausted is None and record.granted_rounds == 0):
            raise ValueError(
                f"run {run_id} is {record.state}"
                + (f" ({record.reason})" if record.reason else "")
                + "; grant-rounds only applies to a run that exhausted its fix rounds"
            )
        if item.state == "running":
            raise ValueError(f"{item.item_id} is running; wait for the run to finish")
        who = by or "operator"
        total = self.store.grant_rounds(run_id, rounds)
        now = self.clock()
        fresh = self.dstore.resume_exhausted(
            item.item_id, run_id, now, f"{rounds} more fix round(s) granted by {who}"
        )
        self._notice(
            "run.rounds_granted",
            f"{who} granted {run_id} {rounds} more fix round(s) ({total} granted in all); "
            f"{item.item_id} resumes on its own PR at the next tick",
            item=item.item_id,
            run=run_id,
            url=record.pr_url,
            by=who,
            rounds=rounds,
            granted_rounds=total,
            pr=record.pr_number,
        )
        self._deliver_report(fresh, by=who)
        return fresh

    # -- state-dir retention -------------------------------------------------------

    def _prune_backups(self) -> None:
        """Keep the newest ``[daemon] backups_keep`` snapshots (0 keeps all)."""
        from sbxloop.backup import prune_backups

        try:
            removed = prune_backups(self.config.paths, keep=self.config.daemon.backups_keep)
        except Exception:
            log.warning("daemon.backup_prune_failed", exc_info=True)
            return
        if removed:
            log.info("daemon.backups_pruned", removed=[b.name for b in removed])

    def _maybe_gc(self, now: float) -> None:
        """Sweep runs/<id>/ on the first tick after start and once a day
        thereafter. Runs before the pause/breaker checks: retention is
        housekeeping, not dispatch, and a paused daemon still fills the disk."""
        if self._last_gc is not None and now - self._last_gc < DAY_S:
            return
        self._last_gc = now
        self.gc(now)

    def gc(self, now: float | None = None) -> None:
        """One retention sweep (see :mod:`sbxloop.gc`); never raises — a
        failed sweep must not take the daemon down with it."""
        self._prune_backups()
        days = self.config.daemon.prune_runs_after_days
        if days <= 0:
            return
        now = self.clock() if now is None else now
        try:
            result = prune_run_dirs(
                self.store,
                self.config.paths,
                older_than_s=days * DAY_S,
                now=now,
                actor="daemon",
            )
        except Exception:
            log.warning(
                "daemon.gc_failed",
                home=str(self.config.home),
                retention_days=days,
                exc_info=True,
            )
            return
        if not result.pruned and not result.failed:
            log.debug("daemon.gc_nothing_to_prune", retention_days=days)
            return
        text = (
            f"daemon.gc: pruned {len(result.pruned)} run dir(s) older than {days:g}d, "
            f"freed {format_bytes(result.bytes_freed)}"
        )
        if result.failed:
            text += f"; {len(result.failed)} could not be removed ({', '.join(result.failed)})"
        self._notice(
            "daemon.gc",
            text,
            pruned=len(result.pruned),
            failed=len(result.failed),
            bytes_freed=result.bytes_freed,
            retention_days=days,
        )

    # -- discovery ---------------------------------------------------------------------

    # Poll backoff doubles per consecutive failure, from one poll interval up
    # to this ceiling; a source that is down for an hour is polled every 30
    # minutes, not every tick.
    SOURCE_BACKOFF_MAX_S = 1800.0

    def _discover(self, now: float) -> int:
        source = self.source
        if now < self._source_next_poll:
            log.debug(
                "source.poll_skipped",
                source=source.name,
                backoff_left_s=self._source_next_poll - now,
            )
            return 0
        started = time.monotonic()
        try:
            found = source.poll()
        except Exception:
            self._source_failures += 1
            delay = min(
                self.config.daemon.poll_interval_s * 2**self._source_failures,
                self.SOURCE_BACKOFF_MAX_S,
            )
            self._source_next_poll = now + delay
            log.warning(
                "source.poll_failed",
                source=source.name,
                failures=self._source_failures,
                next_poll_in_s=round(delay),
                duration_s=round(time.monotonic() - started, 2),
                exc_info=True,
            )
            return 0
        if self._source_failures:
            after = self._source_failures
            self._source_failures = 0
            self._source_next_poll = 0.0
            self._notice(
                "source.poll_recovered",
                f"source {source.name} polling recovered",
                after_failures=after,
            )
        fresh = 0
        for item in found:
            # Per item, because recording one is not worth the daemon. A
            # poll that raises is already caught above and backed off; an
            # item that raises used to travel all the way out of tick() and
            # kill the process, and since discovery is deterministic the
            # next start died on the same item until systemd gave up. One
            # unrecordable item now costs that item, and the run carries on.
            try:
                queued = self.dstore.upsert_new(item, now)
            except Exception:
                log.warning(
                    "item.record_failed",
                    source=source.name,
                    item=item.item_id,
                    repo=item.repo or None,
                    url=item.url or None,
                    hint="item skipped; the rest of the poll is unaffected",
                    exc_info=True,
                )
                continue
            if queued:
                fresh += 1
                self._notice(
                    "item.queued",
                    f"queued {item.item_id}: {item.title}",
                    item=item.item_id,
                    url=item.url or None,
                    title=item.title,
                )
        log.debug(
            "source.polled",
            source=source.name,
            found=len(found),
            new=fresh,
            duration_s=round(time.monotonic() - started, 2),
        )
        return fresh

    # -- schedules (#761, #818) --------------------------------------------------------

    def _ensure_config_schedules(self) -> None:
        """Move every `[[schedules]]` entry of sbxloop.toml into the store
        (#818), once per process and at first sight: a name the store
        already carries keeps the stored schedule — the file is the legacy
        copy, not the source of truth — and the operator is told the
        file's copy is now redundant."""
        with self._schedules_lock:
            if self._config_schedules_imported is not None:
                return
            imported: list[str] = []
            now = self.clock()
            for spec in self.config.schedules:
                if self.dstore.add_schedule(spec, source="config", by=None, now=now):
                    imported.append(spec.name)
                    log.info("schedule.imported", schedule=spec.name, cadence=spec.cadence_text)
                else:
                    log.debug(
                        "schedule.import_skipped", schedule=spec.name, reason="already stored"
                    )
            self._config_schedules_imported = imported
        names = [s.name for s in self.config.schedules]
        if not names:
            return
        fresh = imported
        what = (
            f"imported {', '.join(fresh)} from sbxloop.toml"
            if fresh
            else f"{', '.join(names)} in sbxloop.toml already live in the daemon's database"
        )
        self._notice(
            "daemon.schedules_imported",
            f"📅 schedules: {what} — schedules live in the daemon's database now; remove the "
            "`[[schedules]]` entries from sbxloop.toml (the file's copy is ignored from here on, "
            "and `schedules` / `create_schedule` in chat manage them)",
            schedules=names,
            imported=fresh,
            level="warning",
        )

    def _schedule_tz(self, name: str) -> ZoneInfo:
        self._ensure_config_schedules()
        stored = self.dstore.schedule(name)
        tz = stored.spec.timezone if stored is not None and stored.spec.timezone else None
        return ZoneInfo(tz or self.config.daemon.run_cap_timezone)

    def add_schedule(self, spec: ScheduleConfig, by: str | None, *, source: str) -> str:
        """Create a schedule (#818): live from the next tick, no restart.
        The profile must be one `[[workloads]]` declares — a tick that
        refused at dispatch would fire every cadence — and the name must
        be free. Returns the line to answer with; ``ValueError`` says why
        not."""
        profile = next((p for p in self.config.workloads if p.name == spec.profile), None)
        if profile is None:
            known = ", ".join(p.name for p in self.config.workloads) or "none"
            raise ValueError(
                f"profile {spec.profile!r} is not declared under [[workloads]] (declared: {known})"
            )
        self._ensure_config_schedules()
        now = self.clock()
        if not self.dstore.add_schedule(spec, source=source, by=by, now=now):
            raise ValueError(f"a schedule called {spec.name!r} already exists")
        tz = self._schedule_tz(spec.name)
        next_due = Cadence.parse(spec.every, spec.cron).next_due(now, tz)
        who = by or "operator"
        self._notice(
            "daemon.schedule_added",
            f"📅 schedule {spec.name} created by {who}: {spec.cadence_text} ({tz.key}), "
            f"profile `{spec.profile}`; first tick due {format_due(next_due)}",
            schedule=spec.name,
            by=by,
            source=source,
            cadence=spec.cadence_text,
            profile=spec.profile,
            next_due=next_due,
        )
        return (
            f"schedule {spec.name} created: {spec.cadence_text} ({tz.key}), profile "
            f"`{spec.profile}`; first tick due {format_due(next_due)} — it fires without a "
            "restart."
        )

    def remove_schedule(self, name: str, by: str | None) -> str:
        """Delete a schedule and its state (#818); a tick already queued
        or running is untouched. ``ValueError`` names an unknown one."""
        self._ensure_config_schedules()
        if self.dstore.schedule(name) is None:
            raise ValueError(self._unknown_schedule(name))
        self.dstore.remove_schedule(name)
        who = by or "operator"
        self._notice(
            "daemon.schedule_removed",
            f"🗑 schedule {name} removed by {who}; no further ticks",
            schedule=name,
            by=by,
        )
        return f"schedule {name} removed; no further ticks (a tick already queued still runs)."

    def update_schedule(
        self, name: str, spec: ScheduleConfig, by: str | None, *, source: str
    ) -> str:
        """Atomically replace a stored schedule while retaining its pause and history."""
        profile = next((p for p in self.config.workloads if p.name == spec.profile), None)
        if profile is None:
            known = ", ".join(p.name for p in self.config.workloads) or "none"
            raise ValueError(
                f"profile {spec.profile!r} is not declared under [[workloads]] (declared: {known})"
            )
        self._ensure_config_schedules()
        if self.dstore.schedule(name) is None:
            raise ValueError(self._unknown_schedule(name))
        if not self.dstore.update_schedule(name, spec, now=self.clock()):
            raise ValueError(f"a schedule called {spec.name!r} already exists")
        who = by or "operator"
        self._notice(
            "daemon.schedule_updated",
            f"📅 schedule {name} updated by {who}: {spec.cadence_text}, profile `{spec.profile}`",
            schedule=spec.name,
            previous_name=name,
            by=by,
            source=source,
            cadence=spec.cadence_text,
            profile=spec.profile,
        )
        return f"schedule {spec.name} updated: {spec.cadence_text}, profile `{spec.profile}`."

    # -- the registered repositories ------------------------------------------------

    def _activate_repositories(self) -> None:
        """Apply the registry (importing the file's entries at first sight)
        and remember what this process polls: the enabled set now, which
        is what the sources were built from. The import is narrated when
        the daemon starts (:meth:`narrate_repository_import`), beside
        ``daemon.started``, not here: recovery writes no chronology of its
        own."""
        self.repositories.activate()
        self.polled_repos = frozenset(r.repo.casefold() for r in self.config.enabled_repos())

    def narrate_repository_import(self) -> None:
        """Tell the humans, once, which of the file's entries this process
        imported into the registry, and where registration lives now."""
        imported = self.repositories.activate()
        if not imported:
            return
        self._notice(
            "daemon.repositories_imported",
            f"📦 repositories: imported {', '.join(imported)} from sbxloop.toml — "
            "registration lives in the daemon's database now (`POST`/`PATCH`/"
            "`DELETE /v1/repositories` change it live); a `[[vcs.repos]]` entry still "
            "carries the repository's other settings, and a new entry in the file is "
            "registered at the next start",
            repositories=imported,
        )

    def repository_restart_required(self, entry: RepoConfig) -> bool:
        """Whether the registration's enabled state differs from what this
        process polls, so polling follows at the next start."""
        return (entry.repo.casefold() in self.polled_repos) != entry.enabled

    def add_repository(
        self,
        repo: str,
        *,
        kind: str | None,
        enabled: bool,
        deliver_base: str | None,
        by: str | None,
        source: str,
    ) -> tuple[str, str]:
        """Register a repository: admitted for work now, polled from the
        next start. Returns the name as registered and the line to answer
        with; ``ValueError`` says why not."""
        forge = kind if kind in VCS_KINDS else self.config.vcs.kind
        row = self.repositories.add(
            repo,
            kind=cast(VcsKind | None, kind),
            enabled=enabled,
            deliver_base=deliver_base,
            by=by,
            source=source,
        )
        who = by or "operator"
        polling = (
            "polled from the next daemon start (restart to begin)"
            if enabled
            else "disabled: not polled, not run, until enabled"
        )
        self._notice(
            "daemon.repository_added",
            f"📦 repository {row.repo} ({forge}) registered by {who}; {polling}",
            repo=row.repo,
            forge=forge,
            by=by,
            source=source,
            enabled=enabled,
        )
        return row.repo, f"repository {row.repo} registered; {polling}."

    def update_repository(
        self, repo: str, changes: Mapping[str, Any], *, by: str | None
    ) -> tuple[str, str]:
        """Change a registration's ``enabled`` / ``deliver_base``, live.
        ``KeyError`` names an unknown repository, ``ValueError`` a refused
        change."""
        row = self.repositories.update(repo, changes, by=by)
        who = by or "operator"
        what = ", ".join(f"{key} = {value!r}" for key, value in changes.items()) or "nothing"
        entry = self.config.find_repo(row.repo)
        follows = (
            "; polling follows at the next daemon start"
            if entry is not None and self.repository_restart_required(entry)
            else ""
        )
        self._notice(
            "daemon.repository_updated",
            f"📦 repository {row.repo} changed by {who}: {what}{follows}",
            repo=row.repo,
            by=by,
            changes=dict(changes),
        )
        return row.repo, f"repository {row.repo} updated: {what}{follows}."

    def remove_repository(self, repo: str, *, by: str | None) -> tuple[str, str]:
        """Forget a registration: no longer admitted for work; work already
        queued or running for it is untouched. ``KeyError`` names an
        unknown repository."""
        row = self.repositories.remove(repo, by=by)
        who = by or "operator"
        stops = (
            "; polling stops at the next daemon start"
            if row.repo.casefold() in self.polled_repos
            else ""
        )
        self._notice(
            "daemon.repository_removed",
            f"📦 repository {row.repo} removed by {who}{stops}",
            repo=row.repo,
            by=by,
        )
        return (
            row.repo,
            f"repository {row.repo} removed; work already queued or running for it is "
            f"untouched{stops}.",
        )

    def _fire_schedules(self, now: float) -> int:
        """Queue every schedule tick that has come due since the last one
        handled — at most one per schedule, the latest, recorded at its
        due time so the grid never drifts. A tick whose previous item is
        still live is skipped and said so; a paused schedule's ticks are
        swallowed; a schedule whose profile the config no longer declares
        skips its tick, named. Returns how many items were queued."""
        self._ensure_config_schedules()
        fresh = 0
        profiles = {p.name for p in self.config.workloads}
        for stored in self.dstore.schedules():
            spec = stored.spec
            cadence = Cadence.parse(spec.every, spec.cron)
            tz = self._schedule_tz(spec.name)
            row = self.dstore.schedule_row(spec.name, now)
            due = cadence.latest_due(row.base, now, tz)
            if due is None:
                continue
            when = format_due(due)
            if row.paused_by is not None:
                self.dstore.schedule_due_handled(spec.name, due)
                log.debug("schedule.tick_paused", schedule=spec.name, due=when, by=row.paused_by)
                continue
            if spec.profile not in profiles:
                # Fail closed and loud (#758): a run under a profile that
                # is gone would refuse every need it declares.
                self.dstore.schedule_due_handled(spec.name, due)
                self._notice(
                    "daemon.schedule_skipped",
                    f"⏭ schedule {spec.name}: tick due {when} skipped — its profile "
                    f"`{spec.profile}` is no longer declared under [[workloads]]; declare it "
                    f"again or `schedules remove {spec.name}`",
                    schedule=spec.name,
                    due=when,
                    profile=spec.profile,
                    level="warning",
                )
                continue
            live = self.dstore.live_schedule_item(spec.name)
            if live is not None:
                self.dstore.schedule_due_handled(spec.name, due)
                self._notice(
                    "daemon.schedule_skipped",
                    f"⏭ schedule {spec.name}: tick due {when} skipped — {live.item_id} is "
                    f"still {live.state}" + (f" (run {live.run_id})" if live.run_id else ""),
                    schedule=spec.name,
                    due=when,
                    live_item=live.item_id,
                    live_state=live.state,
                    level="warning",
                )
                continue
            item = self._schedule_item(spec.name, spec.ask, spec.profile, when)
            # The item first, the row second: a crash between the two leaves
            # a queued tick the next pass finds (below) rather than a row
            # claiming a fire that never queued anything.
            queued = self.dstore.upsert_new(item, now)
            self.dstore.schedule_fired(spec.name, due, item.item_id, now)
            if queued:
                fresh += 1
                self._notice(
                    "daemon.schedule_fired",
                    f"⏰ schedule {spec.name}: tick due {when} queued as {item.item_id}",
                    schedule=spec.name,
                    due=when,
                    item=item.item_id,
                    title=item.title,
                )
            else:
                # The tick was queued and the process died before the row
                # recorded it: nothing to add, the row catches up.
                log.info("schedule.tick_exists", schedule=spec.name, item=item.item_id)
        return fresh

    @staticmethod
    def _schedule_item(name: str, ask: str, profile: str, due: str) -> WorkItem:
        title = next((ln.strip() for ln in ask.splitlines() if ln.strip()), name)
        return WorkItem(
            item_id=schedule_item_id(name, due),
            source_key=due,
            title=title if len(title) <= 120 else title[:119] + "…",
            body=ask,
            url="",
            kind="workload",
            profile=profile,
        )

    def schedules(self) -> list[dict[str, Any]]:
        """Every stored schedule with its state, for `schedules`: cadence,
        timezone, profile, ask, provenance, last fire, next due, who
        paused it."""
        self._ensure_config_schedules()
        now = self.clock()
        rows = self.dstore.schedule_rows()
        out: list[dict[str, Any]] = []
        for stored in self.dstore.schedules():
            spec = stored.spec
            cadence = Cadence.parse(spec.every, spec.cron)
            tz = self._schedule_tz(spec.name)
            row = rows.get(spec.name) or ScheduleRow(name=spec.name, anchor=now)
            out.append(
                {
                    "name": spec.name,
                    "cadence": cadence.describe(),
                    "timezone": str(tz.key),
                    "profile": spec.profile,
                    "ask": spec.ask,
                    "source": stored.source,
                    "created_by": stored.created_by,
                    "created_at": stored.created_at,
                    "last_due": row.last_due,
                    "last_fired_at": row.last_fired_at,
                    "last_item": row.last_item,
                    "next_due": cadence.next_due(row.base, tz),
                    "paused_by": row.paused_by,
                    "seen": spec.name in rows,
                }
            )
        return out

    def pause_schedule(self, name: str, by: str | None) -> str:
        """Park one schedule: its ticks are swallowed until `schedules
        resume <name>`. The daemon's own holds are untouched. Returns the
        line to answer with; ``ValueError`` names an unknown schedule."""
        self._ensure_config_schedules()
        if self.dstore.schedule(name) is None:
            raise ValueError(self._unknown_schedule(name))
        now = self.clock()
        self.dstore.schedule_row(name, now)
        who = by or "operator"
        if not self.dstore.set_schedule_paused(name, who, now):
            return f"schedule {name} is already paused."
        self._notice(
            "daemon.schedule_paused",
            f"⏸ schedule {name} paused by {who}; its ticks are skipped until resumed",
            schedule=name,
            by=by,
        )
        return f"schedule {name} paused; its ticks are skipped until `schedules resume {name}`."

    def resume_schedule(self, name: str, by: str | None) -> str:
        """Release a parked schedule: it fires again from its next tick
        (a tick that passed while paused is not made up)."""
        self._ensure_config_schedules()
        stored = self.dstore.schedule(name)
        if stored is None:
            raise ValueError(self._unknown_schedule(name))
        now = self.clock()
        row = self.dstore.schedule_row(name, now)
        if not self.dstore.set_schedule_paused(name, None, now):
            return f"schedule {name} is not paused."
        # Whatever came due while parked is handled, so a daemon that was
        # down for part of the pause does not fire a catch-up on resume.
        spec = stored.spec
        due = Cadence.parse(spec.every, spec.cron).latest_due(
            row.base, now, self._schedule_tz(name)
        )
        if due is not None:
            self.dstore.schedule_due_handled(name, due)
        self._notice(
            "daemon.schedule_resumed",
            f"▶ schedule {name} resumed"
            + (f" by {by}" if by else "")
            + "; fires from its next tick",
            schedule=name,
            by=by,
        )
        return f"schedule {name} resumed; fires from its next tick."

    def _unknown_schedule(self, name: str) -> str:
        known = ", ".join(s.spec.name for s in self.dstore.schedules()) or "none"
        return f"no schedule called {name!r} (stored: {known})"

    # -- dispatch ----------------------------------------------------------------------

    def _dispatch(self, item: WorkItem, *, resume_run_id: str | None) -> TickOutcome:
        """Start one item's run (fresh, or resuming its interrupted run).
        One run at a time, the run is awaited and settled here; with room
        for more, it is left executing and a later tick reaps it."""
        handle = self._launch(item, resume_run_id=resume_run_id)
        if not self._serial:
            return "started"
        self._await(handle)
        return self._settle_run(handle)

    @property
    def agents(self) -> AgentRegistry:
        """The agent registry for the config this loop currently holds."""
        cached = self._agents
        if cached is None or cached[0] is not self.config:
            cached = (self.config, DbAgentRegistry(self.config, self.dstore, clock=self.clock))
            self._agents = cached
        return cached[1]

    def _memory(self, item: WorkItem) -> MemoryService:
        """The memory service for ``item``: the store's memories under the
        item's own config, so a planned assignment and the run it starts
        read the same thing."""
        return MemoryService(
            self.dstore,
            WorkspaceChannelVisibility(self.dstore),
            self._item_config(item).memory,
            self.clock,
        )

    def _assign(self, item: WorkItem, now: float) -> WorkItem:
        """``item`` carrying the agent assignment its run starts with.

        An item already holding a planned assignment keeps it, so every
        attempt at the same work goes to the same agents; otherwise the
        assignment is planned from the lead and roles asked for at
        admission (none: the built-in team) and stored on the item. Each
        binding snapshots its agent's memory block here (S-A5), taken in
        the channel the item names."""
        if is_planned_assignment(item.assignment_json):
            return item
        requested = cast("dict[RunRole, str]", requested_roles(item.assignment_json))
        planned = plan_assignment(
            self.agents,
            kind=item.kind,
            lead=item.lead_agent,
            requested=requested,
            memory=self.memory if self.memory is not None else self._memory(item),
            channel_id=item.channel_id,
        )
        if item.origin_agent is not None or item.chain_depth:
            planned = AgentAssignment(
                lead=planned.lead,
                roles=planned.roles,
                agents=planned.agents,
                channel_id=planned.channel_id,
                origin_agent=item.origin_agent,
                chain_depth=item.chain_depth,
            )
        text = planned.to_json()
        self.dstore.set_item_assignment(item.item_id, text, now)
        log.info(
            "run.assigned",
            item=item.item_id,
            lead=planned.lead,
            roles=dict(planned.roles),
            default=planned.is_default(),
        )
        return item.model_copy(update={"assignment_json": text})

    def _chronicle(
        self, item: WorkItem, run_id: str, item_config: Config | None = None
    ) -> RunChronicle | None:
        """The chronicle telling ``run_id``'s story in the channel that asked
        for ``item``, or None when nobody asked. The poster the API listener
        supplies also lists a run's files; one that does not simply posts
        without them. A resumed segment is numbered by the resumes the run
        has had, so a stop it reaches is said even when an earlier segment
        stopped the same way."""
        posts: object = self.poster
        return RunChronicle.for_item(
            self.poster,
            _item_assignment(item),
            item,
            item_config or self._item_config(item),
            self.clock,
            artifacts=posts if isinstance(posts, RunArtifacts) else None,
            resumes=self.dstore.resumes_for_run(run_id) if self.poster is not None else 0,
        )

    def _chronicle_landed(
        self, item: WorkItem | None, run_id: str, pr: int | None, url: str | None
    ) -> None:
        """A parked run a person approved has merged, outside its engine:
        tell its channel, as the engine's own merge would have."""
        if item is None:
            return
        chronicle = self._chronicle(item, run_id)
        if chronicle is not None:
            chronicle.on_event(Event.now(HostEventTypes.RUN_MERGED, run_id, pr=pr, url=url))

    def _launch(self, item: WorkItem, *, resume_run_id: str | None) -> RunHandle:
        """Mark the item running, build its engine, register the run and
        start its thread. Returns once the run is executing."""
        now = self.clock()
        # A fresh run takes a warm set's id when one is ready (#47), so its
        # provisioning finds the sandboxes already booted and installed.
        run_id = resume_run_id or self._claim_warm(item) or new_run_id()
        if resume_run_id is None:
            self.dstore.mark_running(item.item_id, run_id, now)
            item = self._assign(self.dstore.get(item.item_id) or item, now)
            self.source.report_started(item, run_id)
            # Fresh runs only: a resumed run is pinned to the clone it
            # already has, so moving the source would change nothing.
            if item.recipe is None:
                self._refresh_workspace(self._item_repo(item))
        else:
            self.dstore.mark_resuming(
                item.item_id,
                run_id,
                now,
                provider_recovery=self._provider_recovery().pending(run_id),
            )
            item = self.dstore.get(item.item_id) or item
        log.info(
            "run.dispatch",
            item=item.item_id,
            run=run_id,
            attempt=item.attempts,
            max_attempts=self.config.daemon.max_attempts_per_item,
            resume=resume_run_id is not None,
            title=item.title,
        )
        item_config = self._item_config(item)
        bus = EventBus()
        bus.subscribe(event_log_subscriber)
        # What the run's agents report spending is charged to the pool.
        bus.subscribe(self.usage_pool.subscriber(getattr(item, "channel_id", None)))
        # What the run does is told in the channel that asked for it, under
        # the names of the agents doing it. A resume re-attaches it: the
        # posts a run already made are keyed, so nothing is said twice.
        chronicle = self._chronicle(item, run_id, item_config)
        if chronicle is not None:
            bus.subscribe(chronicle.on_event)
        engine = LoopEngine(
            item_config,
            store=self.store,
            bus=bus,
            sbx=self.sbx,
            worker_python=self.worker_python,
            install_workers=self.install_workers,
            # This daemon watches the item's repository, so a follow-up issue
            # can honestly say which label queues it (#631).
            trigger_label=self.config.labels_for(self._item_repo(item)).trigger,
            # The agents' long-term memory lives in the daemon's store: an
            # agent whose `tools` name `memory` remembers and recalls there.
            memory=MemoryService(
                self.dstore,
                WorkspaceChannelVisibility(self.dstore),
                item_config.memory,
                self.clock,
            ),
        )
        handle = RunHandle(
            item,
            run_id,
            engine,
            bus,
            resume=resume_run_id is not None,
        )
        self._register(handle)
        if self.frontend is not None:
            try:
                self.frontend.run_started(item, run_id, engine, bus)
            except Exception:
                log.warning(
                    "frontend.run_started_failed", item=item.item_id, run=run_id, exc_info=True
                )

        box = handle.outcome
        resume = resume_run_id is not None

        def target() -> None:
            # Context vars are per-thread: stamp run/item on everything the
            # engine logs from here (provisioning, worker client, phases).
            bind_run(run_id, item.item_id, source=self.source.name)
            try:
                box["result"] = self._runner(item, item_config, run_id, bus, resume)
            except BaseException as exc:
                box["error"] = exc
            finally:
                clear_run()

        thread = threading.Thread(target=target, name=f"sbxloop-daemon-run-{run_id}", daemon=True)
        handle.thread = thread
        try:
            thread.start()
        except BaseException:
            with self._current_lock:
                self._runs.pop(run_id, None)
            raise
        return handle

    def _await(self, handle: RunHandle) -> None:
        """Block until ``handle``'s thread ends, honouring an operator
        override from another process while it runs."""
        thread = handle.thread
        assert thread is not None  # nosec B101 - launched
        while thread.is_alive():
            thread.join(timeout=1.0)
            self._poll_overrides([handle])

    def _poll_overrides(self, handles: Sequence[RunHandle]) -> None:
        """`sbxloop daemon abandon|requeue` from another process can only
        touch the row; honour it by cancelling the run, once."""
        for handle in handles:
            thread = handle.thread
            if handle.override_cancel_sent or thread is None or not thread.is_alive():
                continue
            override = self._operator_override(handle.item.item_id, handle.run_id)
            if override is None:
                continue
            log.info(
                "run.cancel_requested",
                item=handle.item.item_id,
                run=handle.run_id,
                reason=f"operator override: item now {override.state}",
            )
            handle.engine.request_cancel()
            handle.override_cancel_sent = True

    def _reap(self) -> tuple[tuple[str, TickOutcome], ...]:
        """Settle every run whose thread has ended, oldest first, on the
        calling (loop) thread; the rest are checked for an operator
        override."""
        handles = self.runs
        if not handles:
            return ()
        settled = [(h.item.item_id, self._settle_run(h)) for h in handles if h.finished]
        self._poll_overrides([h for h in handles if not h.finished])
        return tuple(settled)

    def _settle_run(self, handle: RunHandle) -> TickOutcome:
        """Turn a finished run into what happens to its item, and free its slot."""
        item, run_id, result_box = handle.item, handle.run_id, handle.outcome
        with self._current_lock:
            self._runs.pop(run_id, None)

        error = result_box.get("error")
        result = result_box.get("result")
        log.info(
            "run.finished",
            item=item.item_id,
            run=run_id,
            outcome=(
                result.state
                if result is not None
                else "interrupted"
                if self._stop.is_set()
                else type(error).__name__
                if error is not None
                else "unknown"
            ),
            duration_s=round(time.monotonic() - handle.started, 1),
            attempt=item.attempts,
        )
        # An item-level operator decision (abandon/requeue, possibly from
        # another process) outranks a pending `!sbx cancel`: the row already
        # says what the item's fate is.
        override = self._operator_override(item.item_id, run_id)
        if override is not None:
            stale = self._take_cancel(run_id)
            if stale is not None:
                self._drop_cancel(stale)
            return self._settle_override(item, run_id, override, result_box.get("result"))
        cancel = self._take_cancel(run_id)
        if cancel is not None and not (
            isinstance(error, RunCancelledError) and self._run_is_resumable(run_id)
        ):
            # The cancel came too late: the run finished (or failed) on its
            # own and settles normally below. Its record says so.
            self._finish_cancel_record(cancel, honoured=False, run_id=run_id)
        if (
            cancel is not None
            and isinstance(error, RunCancelledError)
            and self._run_is_resumable(run_id)
        ):
            # The human's cancel took effect (engine raised its cancellation
            # error at a boundary, persisted run left mid-flight). Checked
            # before the shutdown branch: a cancel during quiesce must not be
            # resumed by recovery. Gated on the exception type, not just the
            # persisted state: an infra error re-raised while the run is still
            # resumable looks identical in the store, and a run that finished
            # or genuinely failed after the request settles normally — the
            # cancel simply came too late.
            return self._settle_cancelled(item, run_id, cancel)
        if self._stop.is_set() and "result" not in result_box and self._run_is_resumable(run_id):
            # Interrupted by shutdown at a boundary: the persisted run is
            # still resumable, so leave the item running for recovery. A run
            # that actually FAILED after stop was requested has a terminal
            # persisted state and settles like any failure below — shutdown
            # must not mask genuine errors as "interrupted".
            self.dstore.finish_ledger(run_id, "interrupted", self.clock())
            log.warning(
                "run.interrupted",
                item=item.item_id,
                run=run_id,
                resumable=True,
                hint="shutdown at a boundary; recovery queues it for resume",
            )
            return "interrupted"
        if error is not None and not isinstance(error, SbxloopError | StateError):
            log.error(
                "run.crashed",
                item=item.item_id,
                run=run_id,
                attempt=item.attempts,
                duration_s=round(time.monotonic() - handle.started, 1),
                exc_info=error,
            )
        return self._settle(item, run_id, result, error)

    def _close_run_record(self, run_id: str, reason: str) -> None:
        """Terminate the *run* row for a run this settle just ended for good.

        ``finish_ledger`` closes the daemon's own ledger, but the engine's
        ``runs`` row is written only by the in-process run loop — so a run
        that died inside a phase (a decompose the verify lint rejected, say)
        left ``decomposing`` behind it. Recovery's stale sweep does close it
        eventually, after ``run_stale_after_s``: six hours in the field
        (runs rv2y1a8ke and rq826h546 of item gh:issue:478), and until then
        ``list_runs`` and everything counting active runs disagreed with
        reality — the very mismatch #374 exists to prevent, on a path its
        sweep only reaches by timeout.

        Nothing here is resumable. A requeued item drops its run pin (see
        ``DaemonStore.mark_failed``: queued + ``run_id`` means "resume this
        run"), and a failed item is terminal. Best-effort — the item is
        already settled and no bookkeeping failure may unsettle it.
        """
        try:
            record = self.store.get_run(run_id)
            if record.state in TERMINAL_RUN_STATES:
                return
            self.store.reconcile_run(run_id, "failed", reason)
            self.store.append_event(
                Event.now(
                    "run.reconciled",
                    run_id,
                    state="failed",
                    reason=reason,
                    previous_state=record.state,
                )
            )
            log.info("run.record_closed", run=run_id, previous_state=record.state, reason=reason)
        except (SbxloopError, StateError) as exc:
            log.warning("run.record_close_failed", run=run_id, error=str(exc))

    def _settle_override(
        self,
        item: WorkItem,
        run_id: str,
        fresh: WorkItem,
        result: RunResult | None,
    ) -> TickOutcome:
        """The operator already decided this item's fate while it ran; the
        run's own outcome must not overwrite that (a cancelled run would
        otherwise take the failure path and re-queue an abandoned item).
        Operator decisions never count toward the circuit breaker."""
        now = self.clock()
        report = self._report(run_id, result)
        if fresh.state == "failed":
            self._end_run_cancelled(
                run_id, f"cancelled: {item.item_id} abandoned by operator", source="abandon"
            )
            self.dstore.finish_ledger(run_id, "abandoned", now)
            self._deliver_report(fresh)
            self._frontend_finished(item, report)
            return "failed"
        self._end_run_cancelled(
            run_id, f"cancelled: {item.item_id} requeued by operator", source="requeue"
        )
        self.dstore.finish_ledger(run_id, "requeued", now)
        self._frontend_finished(item, report)
        self._notice(
            "run.requeued",
            f"{item.item_id} requeued by operator; run {run_id} ended {report.state}",
            item=item.item_id,
            run=run_id,
            state=report.state,
        )
        return "requeued"

    def _run_is_resumable(self, run_id: str) -> bool:
        """Whether the run was left mid-flight (interrupted) rather than
        finished. Note RESUMABLE_RUN_STATES includes 'failed' (a failed run
        may be resumed by an operator); an *interruption* is specifically a
        non-terminal state."""
        try:
            return self.store.get_run(run_id).state not in TERMINAL_RUN_STATES
        except SbxloopError:
            # No persisted run yet (died before create_run): nothing to
            # resume, but nothing failed either — recovery re-queues it.
            log.debug("run.no_record", run=run_id, hint="died before create_run")
            return True

    def _settle(
        self,
        item: WorkItem,
        run_id: str,
        result: RunResult | None,
        error: BaseException | None,
    ) -> TickOutcome:
        """Turn how the run ended into what happens to the item.

        ``merged`` is done: the issue closes. ``blocked`` is handed over: the
        run cleared its own bar and GitHub refused, which no further attempt
        would change, so it neither retries nor counts toward the breaker.
        Everything else — a failed run, an error, a run that somehow ended
        ``completed`` with a repository to deliver to — is a failed attempt:
        retried with backoff while the item has attempts left, then given up.
        """
        now = self.clock()
        report = self._report(run_id, result)
        if (result is not None and result.state == "provider_held") or isinstance(
            error, ProviderHeldError
        ):
            reason = str(error) if error else result.reason if result else None
            self.store.set_run_reason(run_id, reason)
            self.store.set_run_state(run_id, "provider_held")
            self.dstore.finish_ledger(run_id, "provider_held", now)
            self.dstore.mark_resume_pending(item.item_id, now)
            self._notice(
                "run.provider_held",
                f"⏸ {item.item_id}: {reason}",
                item=item.item_id,
                run=run_id,
                reason=reason,
            )
            self._frontend_finished(item, report._replace(state="provider_held", reason=reason))
            return "provider_held"
        self._remember_pushed_work(item, report)
        state = result.state if result is not None else None
        if state == "gated":
            return self._settle_gated(item, run_id, report, now)
        if state == "held":
            return self._settle_held(item, run_id, report, now)
        if state == "awaiting_review":
            return self._settle_awaiting_review(item, run_id, report, now)
        hold = self.dstore.review_hold_for(run_id)
        if hold is not None and hold.state in ("open", "approving", "paused", "fixing"):
            # The run came back from a review fix round (#675) and ended
            # some other way: the wait is over, one way or the other.
            self.dstore.resolve_review_hold(
                run_id,
                "merged" if state == "merged" else "dismissed",
                None,
                now,
                None if state == "merged" else f"run ended {state}",
            )
        # Without a repository there is nothing to land: the engine ends
        # `completed` after its gate, and that is the whole job. A workload
        # (#760) ends `completed` once its result is published, whatever
        # `[github]` says — there is no pull request to merge.
        workload = item.kind != "code"
        landed = state == "merged" or (
            state == "completed" and (workload or not self.config.vcs.enabled)
        )
        self._resolve_publish_gate(item, run_id, released=landed, now=now, state=state)
        if landed:
            self.dstore.finish_ledger(run_id, "done", now)
            if self._consecutive_failures:
                log.info("breaker.reset", after_failures=self._consecutive_failures)
            self._set_breaker(None, 0)
            self.dstore.mark_done(
                item.item_id, now, pending_report="completed" if workload else "merged"
            )
            fresh = self.dstore.get(item.item_id) or item
            self._deliver_report(fresh)
            self._frontend_finished(item, report)
            if workload:
                self._notice(
                    "run.done",
                    f"✅ {item.item_id} completed ({report.summary or report.task_summary})",
                    item=item.item_id,
                    run=run_id,
                    tasks=report.task_summary,
                    published=[published_line(entry) for entry in report.published],
                    attempt=item.attempts,
                )
                return "done"
            pr_text = f" · PR {report.pr[1]}" if report.pr and report.pr[1] else ""
            self._notice(
                "run.done",
                f"🎉 {item.item_id} merged ({report.task_summary}){pr_text}",
                item=item.item_id,
                run=run_id,
                url=report.pr[1] if report.pr else None,
                tasks=report.task_summary,
                pr=report.pr[0] if report.pr else None,
                rounds=report.rounds,
                attempt=item.attempts,
            )
            return "done"
        if state == "blocked" or (state == "completed" and self.config.vcs.enabled):
            reason = (
                (result.reason if result is not None else None)
                or ("run ended completed without landing" if state == "completed" else None)
                or "GitHub would not let the loop finish the pull request"
            )
            self.dstore.finish_ledger(run_id, "blocked", now)
            self.dstore.mark_blocked(item.item_id, reason, now)
            fresh = self.dstore.get(item.item_id) or item
            self._deliver_report(fresh)
            self._frontend_finished(item, report._replace(reason=reason))
            pr_text = f" · PR {report.pr[1]}" if report.pr and report.pr[1] else ""
            self._notice(
                "run.blocked",
                f"🚧 {item.item_id} blocked: {reason}{pr_text} — a human needs to look",
                level="error",
                item=item.item_id,
                run=run_id,
                url=report.pr[1] if report.pr else None,
                reason=reason,
                pr=report.pr[0] if report.pr else None,
                hint="the run stopped at something only a human can settle (a gate, a "
                "conflict, a decision the agent must not take); the item stays claimed "
                "until someone acts on it — `sbxloop daemon ctl status` lists what is held",
            )
            return "blocked"
        reason = str(error) if error is not None else (report.reason or f"run ended {report.state}")
        attempts_left = self.config.daemon.max_attempts_per_item - item.attempts
        exhausted = self._exhaustion(run_id, result)
        if exhausted is not None:
            return self._settle_exhausted(item, run_id, exhausted, report, reason, now)
        self._set_breaker(self._breaker_opened_at, self._consecutive_failures + 1)
        self.dstore.finish_ledger(run_id, "failed", now)
        self._close_run_record(run_id, reason)
        if attempts_left > 0:
            self.dstore.mark_failed(item.item_id, reason, now, requeue=True)
            self.source.report_retry(item, reason, attempts_left)
            self._notice(
                "run.failed",
                f"❌ {item.item_id} failed ({reason}); {attempts_left} attempt(s) left",
                level="warning",
                item=item.item_id,
                run=run_id,
                url=report.pr[1] if report.pr else None,
                reason=reason,
                attempt=item.attempts,
                attempts_left=attempts_left,
                retry_backoff_s=self.config.daemon.retry_backoff_s,
                consecutive_failures=self._consecutive_failures,
            )
            outcome: TickOutcome = "retry"
        else:
            self.dstore.mark_failed(item.item_id, reason, now, requeue=False)
            self.source.report_abandoned(item, reason)
            self._notice(
                "run.abandoned",
                f"❌ {item.item_id} abandoned after {item.attempts} attempt(s): {reason}",
                level="error",
                item=item.item_id,
                run=run_id,
                url=report.pr[1] if report.pr else None,
                reason=reason,
                attempts=item.attempts,
                consecutive_failures=self._consecutive_failures,
                hint="the item spent every attempt `[daemon] max_attempts_per_item` "
                "allows and was handed back to its source; nothing retries it on its "
                "own — re-apply the trigger label (or `retry <item>`) to run it again",
            )
            outcome = "failed"
        self._frontend_finished(item, report)
        if self._consecutive_failures >= self.config.daemon.max_consecutive_failures:
            self._set_breaker(now, self._consecutive_failures)
            self._notice(
                "breaker.opened",
                f"🛑 circuit breaker opened after {self._consecutive_failures} consecutive "
                f"failures; pausing dispatch for {self.config.daemon.breaker_cooldown_s:.0f}s",
                level="error",
                consecutive_failures=self._consecutive_failures,
                cooldown_s=self.config.daemon.breaker_cooldown_s,
                hint=_BREAKER_HINT,
            )
        return outcome

    def _remember_pushed_work(self, item: WorkItem, report: RunReport) -> None:
        """Record the branch/PR this attempt pushed to origin, so restarting
        the item by re-applying the trigger label continues that work rather
        than redoing it (#600)."""
        pr_number = report.pr[0] if report.pr else None
        if not report.branch and pr_number is None:
            return
        self.dstore.record_prior_attempt(
            item.item_id, run_id=report.run_id, branch=report.branch, pr_number=pr_number
        )

    def _settle_cancelled(self, item: WorkItem, run_id: str, cancel: CancelRequest) -> TickOutcome:
        now = self.clock()
        report = self._report(run_id, None)._replace(
            state="cancelled", cancelled_by=cancel.requester, requeued=cancel.retry
        )
        self._remember_pushed_work(item, report)
        reason = f"cancelled by {cancel.requester}" + (" (retry)" if cancel.retry else "")
        # The engine state store (state.db) and the daemon store are separate
        # connections, so the run row and the item row cannot share one
        # transaction. The run record is written FIRST and the item writes
        # follow adjacently: an interruption between them leaves a terminal
        # run and a still-running item (which recovery re-queues) rather than
        # the phantom `running` run of #374.
        self._end_run_cancelled(
            run_id,
            reason,
            requester=cancel.requester,
            retry=cancel.retry,
            source="operator_cancel",
        )
        self.dstore.finish_ledger(run_id, "cancelled", now)
        self.dstore.mark_cancelled(item.item_id, reason, now)
        if cancel.retry:
            # cancelled → queued is the same transition `!sbx retry` makes.
            self.dstore.retry(item.item_id, now, reason)
            # report_cancelled(requeued=True) below is the source-side report.
            self.dstore.take_pending_report(item.item_id)
            self._notice(
                "run.cancelled",
                f"⏹ {item.item_id} {reason}; re-queued to run again fresh",
                item=item.item_id,
                run=run_id,
                by=cancel.requester,
                requeued=True,
            )
        else:
            self._notice(
                "run.cancelled",
                f"⏹ {item.item_id} {reason} — `sbxloop resume {run_id}` continues it, "
                f"`!sbx retry {item.item_id}` reruns it fresh",
                item=item.item_id,
                run=run_id,
                by=cancel.requester,
                requeued=False,
            )
        self.source.report_cancelled(item, report)
        self._frontend_finished(item, report)
        self._finish_cancel_record(cancel, honoured=True, run_id=run_id)
        return "cancelled"

    def _settle_gated(
        self, item: WorkItem, run_id: str, report: RunReport, now: float
    ) -> TickOutcome:
        """The run cleared every bar and ``[landing] merge_gate`` parked it:
        free the machinery, persist the gate, tell the humans, move on.

        Not a failure — the breaker resets — and not done: the item waits
        in ``gated`` (invisible to dispatch) until ``approve_merge`` lands
        the PR or ``abandon`` dismisses the gate. The gate row is the
        durable state a restart re-arms prompts from; the watcher snapshot
        happens here, before the finish path drains the watch registry."""
        self.dstore.finish_ledger(run_id, "gated", now)
        if self._consecutive_failures:
            log.info("breaker.reset", after_failures=self._consecutive_failures)
        self._set_breaker(None, 0)
        notify: list[str] = []
        for who in [item.requested_by, *self.dstore.run_watchers(run_id)]:
            if who and who not in notify:
                notify.append(who)
        try:
            record = self.store.get_run(run_id)
        except SbxloopError:
            record = None
        pr_number = record.pr_number if record is not None else None
        pr_url = (record.pr_url if record is not None else None) or ""
        if pr_number is None and report.pr is not None:
            pr_number, pr_url = report.pr
        if pr_number is None:
            # A gate without a PR cannot be approved; hand over instead of
            # parking dead. Should be unreachable — Gated comes after deliver.
            log.error(
                "gate.no_pr",
                run=run_id,
                item=item.item_id,
                hint="a run ended gated but no pull request was recorded for it, so "
                "there is nothing to approve; this should be unreachable — the item is "
                "blocked for a human and the run record is the place to look",
            )
            self.dstore.mark_blocked(item.item_id, "run ended gated without a PR", now)
            fresh = self.dstore.get(item.item_id) or item
            self._deliver_report(fresh)
            self._frontend_finished(item, report)
            return "blocked"
        self.dstore.create_merge_gate(
            run_id,
            item.item_id,
            item.repo or self.config.primary_repo or "",
            pr_number,
            pr_url,
            record.branch if record is not None else report.branch,
            notify,
            uuid.uuid4().hex,
            now,
        )
        self.dstore.mark_gated(item.item_id, now)
        fresh = self.dstore.get(item.item_id) or item
        self._deliver_report(fresh)
        self._frontend_finished(item, report)
        gate = self.dstore.merge_gate_for(run_id)
        if gate is not None:
            self._frontend_gate_opened(item, run_id, gate)
        self._notice(
            "run.gated",
            f"⏸ {item.item_id} ready to merge — waiting for approval · PR #{pr_number} — "
            f"approve in the run's thread or `!sbx merge {item.item_id}` (no deadline)",
            item=item.item_id,
            run=run_id,
            url=pr_url or None,
            pr=pr_number,
        )
        return "gated"

    def _settle_held(
        self, item: WorkItem, run_id: str, report: RunReport, now: float
    ) -> TickOutcome:
        """A workload parked at publishing by its profile's ``publish =
        "hold"`` (#760): judged and kept, nothing delivered. The same
        shape as the merge gate — the machinery is freed, a gate row of
        kind ``publish`` is the durable state, the humans are asked — and
        the release is ``approve_merge`` re-queueing the item with its run
        pinned, so the next tick resumes it at the publishing stage."""
        self.dstore.finish_ledger(run_id, "held", now)
        if self._consecutive_failures:
            log.info("breaker.reset", after_failures=self._consecutive_failures)
        self._set_breaker(None, 0)
        notify: list[str] = []
        for who in [item.requested_by, *self.dstore.run_watchers(run_id)]:
            if who and who not in notify:
                notify.append(who)
        self.dstore.create_merge_gate(
            run_id,
            item.item_id,
            item.repo or "",
            0,
            "",
            None,
            notify,
            uuid.uuid4().hex,
            now,
            kind="publish",
        )
        self.dstore.mark_gated(item.item_id, now, pending_report="held")
        fresh = self.dstore.get(item.item_id) or item
        self._deliver_report(fresh)
        self._frontend_finished(item, report)
        gate = self.dstore.merge_gate_for(run_id)
        if gate is not None:
            self._frontend_gate_opened(item, run_id, gate)
        self._notice(
            "run.held",
            f"⏸ {item.item_id} result held ({report.summary or report.task_summary}) — "
            f"release in the run's thread or `!sbx release {item.item_id}` (no deadline)",
            item=item.item_id,
            run=run_id,
            tasks=report.task_summary,
        )
        return "held"

    def _resolve_publish_gate(
        self, item: WorkItem, run_id: str, *, released: bool, now: float, state: str | None
    ) -> None:
        """A released workload's gate closes with its run (#760):
        ``released`` when the result published, ``dismissed`` when the
        resumed run ended any other way — the retry that follows is a
        fresh run, not this hold."""
        gate = self.dstore.merge_gate_for(run_id)
        if gate is None or gate.kind != "publish" or gate.state not in ("open", "approving"):
            return
        by = gate.resolved_by
        if released:
            self.dstore.resolve_merge_gate(run_id, "released", by, now)
            self._frontend_gate_resolved(item, run_id, gate, "released", by)
            return
        why = f"run ended {state}" if state else "run interrupted"
        self.dstore.resolve_merge_gate(run_id, "dismissed", by, now, detail=why)
        self._frontend_gate_resolved(item, run_id, gate, "dismissed", by, why)

    def _settle_awaiting_review(
        self, item: WorkItem, run_id: str, report: RunReport, now: float
    ) -> TickOutcome:
        """The run cleared every bar of its own and the base wants a review
        the loop cannot give its own PR (#675): free the machinery, persist
        the hold, ask the configured reviewers, tell the humans, move on.

        Like a gate, not a failure — the breaker resets — and not done: the
        item waits in ``awaiting_review`` (invisible to dispatch) while the
        review tick polls the PR; an approval finishes the landing with gh
        ops alone, a request for changes resumes the run for a fix round."""
        self.dstore.finish_ledger(run_id, "awaiting_review", now)
        if self._consecutive_failures:
            log.info("breaker.reset", after_failures=self._consecutive_failures)
        self._set_breaker(None, 0)
        try:
            record = self.store.get_run(run_id)
        except SbxloopError:
            record = None
        pr_number = record.pr_number if record is not None else None
        pr_url = (record.pr_url if record is not None else None) or ""
        if pr_number is None and report.pr is not None:
            pr_number, pr_url = report.pr
        if pr_number is None:
            log.error(
                "review.no_pr",
                run=run_id,
                item=item.item_id,
                hint="a run ended awaiting review but no pull request was recorded for "
                "it, so there is nothing to watch; this should be unreachable — the item "
                "is blocked for a human and the run record is the place to look",
            )
            self.dstore.mark_blocked(item.item_id, "run ended awaiting_review without a PR", now)
            fresh = self.dstore.get(item.item_id) or item
            self._deliver_report(fresh)
            self._frontend_finished(item, report)
            return "blocked"
        repo = item.repo or self.config.primary_repo or ""
        # What the base wants, from the run's own record of the park — or,
        # a person's draft hold (#677), what they want: the PR marked
        # ready, no approval count.
        required, code_owners, draft = 1, False, False
        for _seq, event in self.store.events(run_id, type_prefix="run.awaiting_review"):
            required = max(1, int(event.data.get("approvals_required") or 0))
            code_owners = bool(event.data.get("code_owners"))
            draft = bool(event.data.get("draft"))
        notify = self._review_notify(item, run_id)
        # The loop's identity on this PR, resolved once: the poll excludes
        # its own reviews without an identity read per poll.
        login, is_bot = "", None
        first_park = self.dstore.review_hold_for(run_id) is None
        if self.github is not None:
            try:
                ops = self.github.ops()
                identity = resolve_identity(
                    ops,
                    repo,
                    pr_number,
                    bot_login=self.github.provisioner.gh_bot_login(repo),
                    configured_login=self.config.github.bot_login_for(repo),
                )
                login, is_bot = identity.login, identity.is_bot
                reviewers = self.config.github.reviewers_for(repo)
                if first_park and reviewers and not draft:
                    ops.pr_request_reviewers(repo, pr_number, reviewers)
                    log.info("review.requested", run=run_id, pr=pr_number, reviewers=reviewers)
            except Exception as exc:
                # A missed identity costs nothing but an own-review the
                # poll would exclude anyway; a failed request is one
                # notification the mention below covers.
                self.github.note_failure(exc)
                log.warning("review.setup_failed", run=run_id, pr=pr_number, exc_info=True)
        self.dstore.create_review_hold(
            run_id,
            item.item_id,
            repo,
            pr_number,
            pr_url,
            record.branch if record is not None else report.branch,
            login=login,
            is_bot=is_bot,
            approvals_required=required,
            notify_ids=notify,
            now=now,
            next_poll_at=now + self.config.landing.review_poll_interval_s,
            held_by_draft=draft,
        )
        self.dstore.mark_awaiting_review(item.item_id, now)
        fresh = self.dstore.get(item.item_id) or item
        self._deliver_report(fresh)
        self._frontend_finished(item, report)
        wait_h = self.config.landing.review_wait_s / 3600
        cadence = (
            f"checking every {self.config.landing.review_poll_interval_s / 60:.0f} min "
            f"for {wait_h:.0f} h"
        )
        if draft:
            text = (
                f"✋ {item.item_id} ready to merge, but a person converted PR #{pr_number} "
                f"to draft — holding until it is marked ready for review; I'll finish the "
                f"landing then, or make the changes a reviewer asks for ({cadence})"
            )
        else:
            what = f"{required} approving review{'s' if required != 1 else ''}" + (
                " from a code owner" if code_owners else ""
            )
            text = (
                f"👀 {item.item_id} ready to merge — the base requires {what}; awaiting a "
                f"reviewer on GitHub · PR #{pr_number} — I'll merge once it is approved, or "
                f"make the changes a reviewer asks for ({cadence})"
            )
        self._notice(
            "run.awaiting_review",
            text,
            item=item.item_id,
            run=run_id,
            url=pr_url or None,
            pr=pr_number,
            approvals_required=required,
            code_owners=code_owners,
            draft=draft,
            mention_ids=notify,
        )
        return "awaiting_review"

    def _review_notify(self, item: WorkItem, run_id: str) -> list[str]:
        """Who hears that a PR waits for a review: whoever asked for the
        work, the run's watchers, and ``[landing] review_notify`` for the
        repository (#675)."""
        notify: list[str] = []
        for who in [
            item.requested_by,
            *self.dstore.run_watchers(run_id),
            *self.config.review_notify_for(item.repo or self.config.primary_repo or ""),
        ]:
            if who and who not in notify:
                notify.append(who)
        return notify

    # -- review holds: the poll and its exits (#675) ---------------------------------

    def _review_tick(self, now: float) -> None:
        """Poll each parked PR whose poll is due: two requests (the PR and
        its reviews) per hold per ``[landing] review_poll_interval_s``. An
        approval by enough humans finishes the landing on a thread with gh
        ops alone; a human's request for changes hands the run back to the
        engine; a PR closed by hand settles; a wait past ``review_wait_s``
        pauses the hold until ``resume <item>``."""
        if self.github is None:
            return
        for hold in self.dstore.due_review_holds(now):
            try:
                self._poll_review_hold(hold, now)
            except Exception:
                log.warning("review.poll_failed", run=hold.run_id, exc_info=True)

    def _poll_review_hold(self, hold: ReviewHold, now: float) -> None:
        assert self.github is not None
        cfg = self.config.landing
        run_id, item_id = hold.run_id, hold.item_id
        if now - hold.since_at >= cfg.review_wait_s:
            waited = (now - hold.since_at) / 3600
            why = f"no review after {waited:.0f} h; resume {item_id} to keep waiting"
            self.dstore.pause_review_hold(run_id, now, why)
            self.dstore.mark_paused_review(item_id, why, now)
            self._notice(
                "run.review_paused",
                f"💤 {item_id}: PR #{hold.pr_number} has waited {waited:.0f} h for a review with "
                f"no verdict; I've stopped checking — `resume {item_id}` re-arms the wait, "
                "`abandon` gives the PR up (it stays open either way)",
                level="warning",
                item=item_id,
                run=run_id,
                url=hold.pr_url or None,
                pr=hold.pr_number,
                mention_ids=hold.notify_ids,
            )
            return
        self.dstore.review_hold_polled(run_id, now + cfg.review_poll_interval_s)
        ops = self.github.ops()
        try:
            pr = ops.pr_get(hold.repo, hold.pr_number)
            verdicts = ops.pr_review_verdicts(
                hold.repo, hold.pr_number, exclude=(hold.login, hold.is_bot)
            )
        except Exception as exc:
            self.github.note_failure(exc)
            log.warning("review.poll_failed", run=run_id, pr=hold.pr_number, exc_info=True)
            return
        merged = bool(pr.get("merged"))
        closed = str(pr.get("state") or "").lower() == "closed" and not merged
        draft = bool(pr.get("draft"))
        changes = [v.login for v in verdicts if v.state == "CHANGES_REQUESTED" and not v.is_bot]
        approvals = [v.login for v in verdicts if v.state == "APPROVED" and not v.is_bot]
        log.debug(
            "review.polled",
            run=run_id,
            pr=hold.pr_number,
            approvals=approvals,
            changes_requested=changes,
            merged=merged,
            closed=closed,
            draft=draft,
        )
        if closed:
            if not self.dstore.claim_review_hold(run_id, "approving"):
                return
            self.dstore.resolve_review_hold(
                run_id, "dismissed", None, now, "PR closed unmerged while awaiting review"
            )
            try:
                self.dstore.abandon(item_id, "PR closed unmerged while awaiting review", now)
            except ValueError:
                log.warning("review.abandon_failed", item=item_id, exc_info=True)
            fresh = self.dstore.get(item_id)
            if fresh is not None:
                self._deliver_report(fresh)
            self._notice(
                "review.dismissed",
                f"🚪 {item_id}: PR #{hold.pr_number} was closed unmerged while awaiting review",
                item=item_id,
                run=run_id,
                url=hold.pr_url or None,
                pr=hold.pr_number,
            )
            return
        if changes and not merged:
            if not self.dstore.claim_review_hold(run_id, "fixing"):
                return
            who = ", ".join(changes)
            try:
                self.dstore.resume_for_fix(
                    item_id, run_id, now, f"changes requested by {who}; resuming for a fix round"
                )
            except ValueError:
                log.warning("review.resume_failed", item=item_id, exc_info=True)
                self.dstore.reopen_review_hold(run_id, now, "could not re-queue the item")
                return
            self._notice(
                "review.changes_requested",
                f"✏️ {item_id}: {who} requested changes on PR #{hold.pr_number} — "
                "resuming the run for a fix round at the next tick",
                item=item_id,
                run=run_id,
                url=hold.pr_url or None,
                pr=hold.pr_number,
                by=who,
            )
            return
        if hold.held_by_draft and not merged:
            # A person's draft hold (#677): the PR marked ready ends it —
            # the landing re-runs every bar, and a review the base wants
            # parks it again, as a review wait this time.
            if draft:
                return
            if not self.dstore.claim_review_hold(run_id, "approving"):
                return
            by = ", ".join(approvals) or "GitHub"
            self._notice(
                "review.ready",
                f"🚀 {item_id}: PR #{hold.pr_number} was marked ready for review — "
                "completing the landing",
                item=item_id,
                run=run_id,
                url=hold.pr_url or None,
                pr=hold.pr_number,
            )
        elif merged or len(approvals) >= hold.approvals_required:
            if not self.dstore.claim_review_hold(run_id, "approving"):
                return
            by = ", ".join(approvals) or "GitHub"
            self._notice(
                "review.approved",
                f"✅ {item_id}: PR #{hold.pr_number} approved by {by} — completing the landing",
                item=item_id,
                run=run_id,
                url=hold.pr_url or None,
                pr=hold.pr_number,
                by=by,
            )
        else:
            return
        threading.Thread(
            target=self._complete_review_landing,
            args=(hold, by),
            daemon=True,
            name=f"sbxloop-review-{run_id}",
        ).start()

    def resume_review(self, target: str, by: str | None = None) -> str:
        """Operator ``resume <item|run>`` on a review wait (#675): check the
        PR now and start the wait over — the way to re-arm a paused hold,
        or to poll at once instead of at the next interval."""
        recovery = self._provider_recovery()
        if target == self.config.agent.backend and recovery.hold() is not None:
            recovery.release()
            return f"{target}: provider hold released by {by or 'operator'}"
        item = self._provider_item(target)
        if item is not None:
            recovery.release()
            self.dstore.mark_resume_pending(item.item_id, self.clock())
            return f"{item.item_id}: provider hold released; continuing run {item.run_id}"
        hold = self.dstore.review_hold_for(target.strip())
        if hold is None:
            raise ValueError(f"{target!r} is not waiting for a review")
        if hold.state in ("merged", "dismissed"):
            raise ValueError(
                f"{hold.item_id}'s review wait ended ({hold.state}); `retry {hold.item_id}` "
                "runs the item again from scratch"
            )
        if hold.state in ("approving", "fixing"):
            raise ValueError(
                f"{hold.item_id} is already past the wait ({hold.state}); nothing to resume"
            )
        now = self.clock()
        self.dstore.reopen_review_hold(hold.run_id, now, None, restart=True)
        if hold.state == "paused":
            self.dstore.mark_awaiting_review(hold.item_id, now)
        who = by or "operator"
        self._notice(
            "run.review_resumed",
            f"👀 {hold.item_id}: waiting for a review on PR #{hold.pr_number} again "
            f"({who}); checking it now",
            item=hold.item_id,
            run=hold.run_id,
            url=hold.pr_url or None,
            pr=hold.pr_number,
            by=who,
        )
        return (
            f"{hold.item_id} is waiting for a review on PR #{hold.pr_number} again; "
            "I'll check it at the next tick."
        )

    def _complete_review_landing(self, hold: ReviewHold, by: str) -> None:
        """Finish an approved review wait: :func:`land` with gh ops alone,
        as for a merge gate. A landing GitHub still refuses (a review that
        went stale, a check that turned red) puts the hold back up."""
        run_id, item_id = hold.run_id, hold.item_id
        ended = "it was marked ready" if hold.held_by_draft else "its approval"
        try:
            outcome = self._land_parked(hold.repo, hold.pr_number, hold.branch, run_id)
        except RunCancelledError as exc:
            self.dstore.reopen_review_hold(run_id, self.clock(), str(exc))
            return
        except Exception as exc:
            assert self.github is not None
            self.github.note_failure(exc)
            self.dstore.reopen_review_hold(run_id, self.clock(), f"landing failed: {exc}")
            self._notice(
                "review.merge_failed",
                f"⚠ landing {item_id} after {ended} did not merge: {exc} — still "
                "waiting; I'll try again at the next check",
                level="warning",
                item=item_id,
                run=run_id,
            )
            return
        now = self.clock()
        if isinstance(outcome, Landed):
            how = (
                "merged once marked ready"
                if hold.held_by_draft
                else f"merged after approval by {by}"
            )
            try:
                self.store.reconcile_run(run_id, "merged", how)
            except SbxloopError:
                log.warning("review.record_update_failed", run=run_id, exc_info=True)
            self.dstore.resolve_review_hold(run_id, "merged", by, now)
            self._file_followups(run_id, item_id, hold.repo)
            self.dstore.mark_done(item_id, now, pending_report="merged")
            self.dstore.finish_ledger(run_id, "done", now)
            fresh = self.dstore.get(item_id)
            if fresh is not None:
                self._deliver_report(fresh)
            self._chronicle_landed(fresh, run_id, hold.pr_number, hold.pr_url or None)
            self._notice(
                "run.done",
                f"🎉 {item_id} {how} · PR #{hold.pr_number}",
                item=item_id,
                run=run_id,
                url=hold.pr_url or None,
                pr=hold.pr_number,
                by=by,
                mention_ids=hold.notify_ids,
            )
        elif isinstance(outcome, Closed):
            self.dstore.resolve_review_hold(
                run_id, "dismissed", by, now, "PR closed unmerged while awaiting review"
            )
            try:
                self.dstore.abandon(item_id, "PR closed unmerged while awaiting review", now)
            except ValueError:
                log.warning("review.abandon_failed", item=item_id, exc_info=True)
            fresh = self.dstore.get(item_id)
            if fresh is not None:
                self._deliver_report(fresh)
            self._notice(
                "review.dismissed",
                f"🚪 {item_id}: PR #{hold.pr_number} was closed unmerged while awaiting review",
                item=item_id,
                run=run_id,
            )
        elif isinstance(outcome, AwaitingReview):
            # Parked again, maybe for the other reason (#677): a draft
            # hold lifted and the base wants a review, or a reviewer's
            # approval met a PR someone converted back to draft. The row
            # waits for what the landing now says.
            why = f"the base now wants {outcome.wanted}"
            self.dstore.reopen_review_hold(
                run_id,
                now,
                why,
                held_by_draft=outcome.draft,
                approvals_required=max(1, outcome.approvals_required),
            )
            self._notice(
                "review.reparked",
                f"👀 landing {item_id} did not merge yet: {why} — still waiting",
                item=item_id,
                run=run_id,
                url=hold.pr_url or None,
                pr=hold.pr_number,
            )
        else:
            why = outcome.why if isinstance(outcome, Blocked | NeedsFix) else str(outcome)
            self.dstore.reopen_review_hold(run_id, now, why)
            self._notice(
                "review.merge_failed",
                f"⚠ landing {item_id} after {ended} did not merge: {why} — still "
                "waiting; I'll try again at the next check",
                level="warning",
                item=item_id,
                run=run_id,
                url=hold.pr_url or None,
                pr=hold.pr_number,
            )

    def approve_merge(
        self, target: str, by: str | None = None, *, expected_revision: int | None = None
    ) -> str:
        """One human approval for a parked merge (``[landing] merge_gate``).

        Fast and event-loop-safe: resolve the gate, win (or lose) the CAS,
        spawn the gh-ops-only landing thread, answer in prose. Refusals
        raise ``ValueError`` with the reason. With ``expected_revision``
        (a remote client's, #1038) the approval binds to that revision of
        the gate: the swap to ``approving`` requires it, a gate that moved
        is ``stale_revision``, and a lost swap is ``already_in_progress``
        — typed refusals, since the caller is not a person reading prose."""
        gate = self.dstore.merge_gate_for(target.strip())
        if gate is None:
            raise ValueError(f"no merge gate for {target!r} — nothing is awaiting approval")
        if expected_revision is not None and gate.revision != expected_revision:
            raise ControlError(
                "stale_revision",
                f"gate for {gate.item_id} is at revision {gate.revision}, not {expected_revision}",
                revision=gate.revision,
            )
        if gate.kind == "publish":
            return self._release_hold(gate, by, expected_revision=expected_revision)
        if gate.state == "merged":
            raise ValueError(f"{gate.item_id} already merged (PR #{gate.pr_number})")
        if gate.state == "dismissed":
            raise ValueError(
                f"{gate.item_id}'s merge gate was dismissed; `retry {gate.item_id}` "
                "runs the item again from scratch"
            )
        if self.github is None:
            raise ValueError("this daemon has no github handle to merge with")
        who = by or "operator"
        if not self.dstore.claim_merge_gate(gate.run_id, who, expected_revision=expected_revision):
            self._lost_gate_swap(gate, expected_revision)
            return f"{gate.item_id} is already being merged — hold on."
        thread = threading.Thread(
            target=self._complete_landing,
            args=(gate, who),
            daemon=True,
            name=f"sbxloop-merge-{gate.run_id}",
        )
        self._landing_threads = [t for t in self._landing_threads if t.is_alive()] + [thread]
        thread.start()
        return (
            f"✅ approved by {who} — completing the landing of PR #{gate.pr_number} "
            "(update if behind → checks → merge); I'll report in the run's thread."
        )

    def _lost_gate_swap(self, gate: MergeGate, expected_revision: int | None) -> None:
        """A revision-bound approval lost the swap: say why, typed. A prose
        caller (no revision) keeps its sentence."""
        if expected_revision is None:
            return
        fresh = self.dstore.merge_gate_for(gate.run_id) or gate
        if fresh.revision != expected_revision:
            raise ControlError(
                "stale_revision",
                f"gate for {gate.item_id} is at revision {fresh.revision}, not {expected_revision}",
                revision=fresh.revision,
            )
        raise ControlError(
            "already_in_progress",
            f"gate for {gate.item_id} is {fresh.state}: another approval got there first",
        )

    def _release_hold(
        self, gate: MergeGate, by: str | None, *, expected_revision: int | None = None
    ) -> str:
        """Release a workload held at publishing (#760): win the CAS, put
        the item back in the queue with its run pinned, and let the next
        tick resume the run at its publishing stage — the engine's own
        publish, on a fresh pair, idempotent over what already went out.
        No thread: the tick is the executor."""
        if gate.state == "released":
            raise ValueError(f"{gate.item_id} already released and published")
        if gate.state == "dismissed":
            raise ValueError(
                f"{gate.item_id}'s held result was dropped; `retry {gate.item_id}` "
                "runs the item again from scratch"
            )
        who = by or "operator"
        if not self.dstore.claim_merge_gate(gate.run_id, who, expected_revision=expected_revision):
            self._lost_gate_swap(gate, expected_revision)
            return f"{gate.item_id} is already being released — hold on."
        now = self.clock()
        try:
            self.dstore.resume_for_release(gate.item_id, gate.run_id, now, who)
        except ValueError as exc:
            self.dstore.reopen_merge_gate(gate.run_id, str(exc))
            raise
        self._notice(
            "run.released",
            f"▶ {gate.item_id} released by {who} — publishing on the next tick",
            item=gate.item_id,
            run=gate.run_id,
            by=who,
        )
        return f"✅ released by {who} — {gate.item_id} publishes on the next tick."

    def _merge_tick(self, waiting: str) -> None:
        """The approve thread's wait between GitHub polls: bounded sleeps,
        cut short only by daemon shutdown (the boot reconcile then puts the
        gate back up)."""
        if self._stop.is_set() and not self._graceful:
            raise RunCancelledError("daemon stopping — approve again after the restart")
        time.sleep(min(self.config.landing.ci_poll_interval_s, 30.0))

    def _join_landings(self) -> None:
        """An operator's ``stop`` waits for the landings in flight: a merge
        cut short between update-branch and merge is the one state a
        restart cannot make right on its own."""
        for thread in self._landing_threads:
            if not thread.is_alive():
                continue
            log.info("daemon.stop_waits_for_landing", thread=thread.name)
            thread.join(timeout=self.config.daemon.shutdown_grace_s)
            if thread.is_alive():
                log.warning(
                    "daemon.stop_landing_timeout",
                    thread=thread.name,
                    grace_s=self.config.daemon.shutdown_grace_s,
                    hint="the gate is re-armed at the next start",
                )
        self._landing_threads = []

    def _complete_landing(self, gate: MergeGate, by: str) -> None:
        """Finish a parked landing with gh ops alone — no sandbox pair, no
        engine. ``land()`` re-runs every bar (undraft, objections, CI,
        update-branch, reconciliation) against the live PR, so a review or
        comment left during the park is honoured, never merged over."""
        run_id, item_id = gate.run_id, gate.item_id
        self._notice(
            "gate.approved",
            f"✅ merge of {item_id} approved by {by} — completing the landing",
            item=item_id,
            run=run_id,
            url=gate.pr_url or None,
            by=by,
        )
        assert self.github is not None  # approve_merge refused without one
        try:
            outcome = self._land_parked(gate.repo, gate.pr_number, gate.branch, run_id)
        except RunCancelledError as exc:
            self.dstore.reopen_merge_gate(run_id, str(exc))
            return
        except Exception as exc:
            self.github.note_failure(exc)
            self.dstore.reopen_merge_gate(run_id, f"approval failed: {exc}")
            item = self.dstore.get(item_id)
            if item is not None:
                self._frontend_gate_resolved(item, run_id, gate, "failed", by, str(exc))
            self._notice(
                "gate.merge_failed",
                f"⚠ approving {item_id} did not land: {exc} — the gate is back up; "
                "fix and approve again",
                level="warning",
                item=item_id,
                run=run_id,
            )
            return
        now = self.clock()
        item = self.dstore.get(item_id)
        if isinstance(outcome, Landed):
            try:
                self.store.reconcile_run(run_id, "merged", f"merge approved by {by}")
            except SbxloopError:
                log.warning("gate.record_update_failed", run=run_id, exc_info=True)
            self.dstore.resolve_merge_gate(run_id, "merged", by, now)
            self._file_followups(run_id, item_id, gate.repo)
            self.dstore.mark_done(item_id, now, pending_report="merged")
            self.dstore.finish_ledger(run_id, "done", now)
            fresh = self.dstore.get(item_id)
            if fresh is not None:
                self._deliver_report(fresh)
            if item is not None:
                self._frontend_gate_resolved(item, run_id, gate, "merged", by, outcome.sha)
            self._chronicle_landed(fresh or item, run_id, gate.pr_number, gate.pr_url or None)
            self._notice(
                "run.done",
                f"🎉 {item_id} merged after approval by {by} · PR #{gate.pr_number}",
                item=item_id,
                run=run_id,
                url=gate.pr_url or None,
                pr=gate.pr_number,
                by=by,
            )
        elif isinstance(outcome, Closed):
            self.dstore.resolve_merge_gate(
                run_id, "dismissed", by, now, detail="PR closed unmerged while parked"
            )
            try:
                self.dstore.abandon(item_id, "PR closed unmerged while parked", now)
            except ValueError:
                log.warning("gate.abandon_failed", item=item_id, exc_info=True)
            fresh = self.dstore.get(item_id)
            if fresh is not None:
                self._deliver_report(fresh)
            if item is not None:
                self._frontend_gate_resolved(
                    item, run_id, gate, "dismissed", by, "PR closed unmerged"
                )
            self._notice(
                "gate.dismissed",
                f"🚪 {item_id}: its PR was closed unmerged while parked; gate dismissed",
                item=item_id,
                run=run_id,
            )
        else:
            why = outcome.why if isinstance(outcome, Blocked | NeedsFix) else str(outcome)
            self.dstore.reopen_merge_gate(run_id, why)
            if item is not None:
                self._frontend_gate_resolved(item, run_id, gate, "failed", by, why)
            self._notice(
                "gate.merge_failed",
                f"⚠ approving {item_id} did not land: {why} — the gate is back up; "
                "fix and approve again",
                level="warning",
                item=item_id,
                run=run_id,
            )

    def _file_followups(self, run_id: str, item_id: str, repo: str) -> None:
        """File the follow-ups of a parked run the daemon just merged (#517).

        The engine files them when its landing parks; this pass covers what
        that one could not (a GitHub failure, a restart) and is idempotent
        against it: the run's ``followup`` rows and the issue markers on the
        repository mean nothing is filed twice. Best-effort: the PR is
        merged, so a failure here is logged, never raised."""
        assert self.github is not None
        try:
            item = self.dstore.get(item_id)
            cfg = self._item_config(item) if item is not None else self.config
            bus = EventBus()
            bus.subscribe(self.store.append_event)
            bus.subscribe(event_log_subscriber)
            filer = FollowupFiler(
                self.github.ops(),
                repo,
                self.store,
                bus,
                cfg,
                trigger_label=self.config.labels_for(repo).trigger,
            )
            filer.file(
                self.store.get_run(run_id),
                recorded_review_rounds(self.store, run_id),
                issues_enabled=None,
            )
        except Exception:
            log.warning("gate.followups_failed", run=run_id, item=item_id, exc_info=True)

    def _land_parked(
        self, repo: str, number: int, branch: str | None, run_id: str
    ) -> LandingOutcome:
        """The gh-ops-only landing a parked run finishes with — a merge gate
        approved, a review wait approved (#675), a draft hold lifted (#677).
        ``land()`` re-runs every bar (objections, CI, update-branch,
        reconciliation) against the live PR, so a review or comment left
        during the park is honoured, never merged over — and a draft is a
        person's (``own_draft=False``): the loop's own was cleared by the
        run's landing, so one now is a hold, not something to un-draft."""
        assert self.github is not None
        ops = self.github.ops()
        identity = resolve_identity(
            ops,
            repo,
            number,
            bot_login=self.github.provisioner.gh_bot_login(repo),
            configured_login=self.config.github.bot_login_for(repo),
        )
        login = identity.login
        base = ops.pr_get(repo, number).get("base")
        base_ref = str(base.get("ref") or "") if isinstance(base, dict) else ""
        if not base_ref:
            base_ref = self.config.github.for_repo(repo).deliver_base or ops.default_branch(repo)
        return land(
            ops,
            repo,
            number,
            cfg=self.config.landing,
            branch=branch,
            node_id=None,
            login=login,
            is_bot=identity.is_bot,
            update=UpdateState(),
            on_update=lambda state: None,
            tick=self._merge_tick,
            emit=lambda type, **data: log.info(type, run=run_id, **data),
            answered=self.store.answered_objections(run_id),
            review_posted=True,
            own_draft=False,
            ack=lambda threads: acknowledge_human_threads(
                ops,
                repo,
                number,
                run_id=run_id,
                login=login,
                threads=threads,
                is_bot=identity.is_bot,
            ),
            # The same judgment the run's landing made (#611): the PR's
            # own base, its merge base, and the advisory rounds spent.
            policy_for=check_policy_reader(
                ops,
                repo,
                base_ref,
                cfg=self.config.landing,
                advisory_spent=self.store.advisory_rounds(run_id),
                number=number,
            ),
            bot_round_spent=self.store.bot_round_spent(run_id),
        )

    def _reconcile_gates(self) -> None:
        """An ``approving`` gate at boot is an approval a dead process took
        and never finished: put the gate back up and say so — the click or
        command works again. (`land()` is idempotent against a PR that did
        merge: the next approval sees ``merged`` and settles ``Landed``.)"""
        for gate in self.dstore.open_merge_gates():
            if gate.state != "approving" or gate.kind == "publish":
                # A released hold (#760) is a queued item with its run
                # pinned: the next tick resumes it, nothing to put back up.
                continue
            self.dstore.reopen_merge_gate(
                gate.run_id, "approval interrupted by a restart — approve again"
            )
            self._notice(
                "gate.merge_failed",
                f"⚠ {gate.item_id}: a merge approval was interrupted by a restart — "
                "the gate is back up; approve again",
                level="warning",
                item=gate.item_id,
                run=gate.run_id,
            )

    def _reconcile_review_holds(self) -> None:
        """An ``approving`` review hold at boot is a landing a dead process
        started and never finished (#675): put the wait back up; the next
        poll sees the approval again (or ``merged``) and lands."""
        for hold in self.dstore.review_holds(("approving",)):
            self.dstore.reopen_review_hold(
                hold.run_id, self.clock(), "landing interrupted by a restart"
            )
            log.warning("review.landing_interrupted", run=hold.run_id, item=hold.item_id)

    def _frontend_gate_opened(self, item: WorkItem, run_id: str, gate: MergeGate) -> None:
        if self.frontend is not None:
            try:
                self.frontend.merge_gate_opened(item, run_id, gate)
            except Exception:
                log.warning("frontend.gate_opened_failed", run=run_id, exc_info=True)

    def _frontend_gate_resolved(
        self,
        item: WorkItem,
        run_id: str,
        gate: MergeGate,
        outcome: str,
        by: str | None,
        detail: str | None = None,
    ) -> None:
        if self.frontend is not None:
            try:
                self.frontend.merge_gate_resolved(item, run_id, gate, outcome, by, detail)
            except Exception:
                log.warning("frontend.gate_resolved_failed", run=run_id, exc_info=True)

    def _live_probe(self, run_id: str | None) -> str | None:
        """``run_id`` while that run is still in flight, else ``None``."""
        if run_id is None or all(handle.run_id != run_id for handle in self.runs):
            return None
        return run_id

    def _bind_probes(self, item_id: str) -> None:
        """The run just launched for ``item_id`` is the probe of a half-open
        breaker or provider hold that has none yet. An item that settled at
        once is no probe: the next launch becomes it."""
        handle = next((h for h in self.runs if h.item.item_id == item_id), None)
        if handle is None:
            return
        if self._breaker_half_open and self._breaker_probe is None:
            self._breaker_probe = handle.run_id
        if self._provider_half_open and self._provider_probe is None:
            self._provider_probe = handle.run_id

    def _set_breaker(self, opened_at: float | None, consecutive_failures: int) -> None:
        # Any transition ends a half-open window (re-opened, reset, or a
        # failure counted); the half-open transition itself re-marks it.
        self._breaker_half_open = False
        self._breaker_probe = None
        self._breaker_opened_at = opened_at
        self._consecutive_failures = consecutive_failures
        self.dstore.set_breaker(opened_at, consecutive_failures)

    def reset_breaker(self, by: str | None = None) -> dict[str, Any]:
        """Operator: close the breaker and zero its failure count now rather
        than wait out ``breaker_cooldown_s``. Releasing holds never touches
        the breaker; this is the one operator path that does. Persisted like
        every breaker transition, so a restart keeps it closed."""
        was_open = self._breaker_opened_at is not None
        failures = self._consecutive_failures
        self._set_breaker(None, 0)
        holds = self.holds
        if was_open or failures:
            who = by or "operator"
            self._notice(
                "breaker.reset",
                f"{who} reset the circuit breaker ({failures} consecutive failure(s) cleared)"
                + (f"; still paused by {', '.join(holds)}" if holds else "; dispatching again"),
                by=who,
                after_failures=failures,
                was_open=was_open,
            )
        return {"was_open": was_open, "consecutive_failures": failures, "holds": holds}

    def _breaker_open(self, now: float) -> bool:
        if self._breaker_opened_at is None:
            return False
        if now - self._breaker_opened_at >= self.config.daemon.breaker_cooldown_s:
            # Half-open: allow one item through; a success resets, a failure
            # re-opens via the counter.
            self._set_breaker(None, max(self._consecutive_failures - 1, 0))
            self._breaker_half_open = True
            self._notice(
                "breaker.half_open",
                "circuit breaker half-open; allowing one item",
                consecutive_failures=self._consecutive_failures,
            )
            return False
        return True

    # -- item -> run mapping ---------------------------------------------------------

    def _item_repo(self, item: WorkItem) -> str | None:
        """The ``owner/name`` this item's run must act on.

        The item carries it since multi-repo discovery; rows written before
        that (and legacy ``gh:<n>`` ids) carry none, and fall back to the
        configured default — the single-repo behaviour, unchanged.
        """
        repo = item.repo
        if repo is None:
            parsed = try_parse_gh_id(item.item_id)
            repo = parsed.repo if parsed is not None else None
        if repo is not None and self.config.find_repo(repo) is None:
            log.warning("run.unknown_item_repo", item=item.item_id, repo=repo)
            return None
        return repo

    def _item_config(self, item: WorkItem) -> Config:
        if item.recipe is not None:
            # A recipe narrows the config itself, at start, inside the
            # runner's exception boundary — so a target removed while the
            # item was queued is reported and settled normally. A resume
            # must first rehydrate the run's persisted profile.
            return self.config.model_copy(update={"keep_on_failure": False})
        # Narrow the section to the item's repository first, so the run's
        # per-repo deliver_base / token_env win over the global defaults.
        item_repo = self._item_repo(item)
        # The issue that queued the item, if one did: a chat item's key is
        # a message id (#760), a schedule tick's its due time (#761).
        issue = (
            int(item.source_key)
            if item.source_key.isdigit() and not is_local_id(item.item_id)
            else None
        )
        gh = GithubConfig.model_validate(
            {
                **self.config.github.for_repo(
                    item_repo, workspace=self.config.workspace_for_repo(item_repo)
                ).model_dump(),
                "create_repo": False,
                # "Closes #N" in the PR body: GitHub links issue and PR and
                # closes the issue on merge even when the daemon is not
                # running to do it. A workload's issue closes when its
                # result lands (`report_completed`), not with a PR.
                "deliver_closes": issue if item.kind == "code" else None,
            }
        )
        update: dict[str, Any] = {
            "github": gh,
            # The declared list follows the section's view (#2255).
            "vcs": self.config.vcs.model_copy(update={"repos": list(gh.repos)}),
            "keep_on_failure": False,
        }
        if item.kind == "workload" and issue is not None:
            # The workload's issue sink answers on the issue that asked
            # (#760) rather than filing a new one.
            update["workload"] = self.config.workload.model_copy(update={"result_issue": issue})
        sandbox = self.config.sandbox
        daemon_mode = self.config.daemon.workspace_isolation
        if sandbox.workspace_isolation != daemon_mode and (
            self._workspace_checkout(item_repo) is not None
            or (daemon_mode == "in-place" and self.config.workspace_for_repo(item_repo) is not None)
        ):
            # Unattended runs answer the dirty-tree question by config
            # (#255): `auto`'s refusal has no human to act on it and would
            # fail every issue while someone has uncommitted work in the
            # checkout. Only a git checkout gets the override, except an
            # explicit 'in-place', which is the one mode that runs in a
            # plain directory as it is. Forcing `clone` on a plain directory
            # would turn every run into a provisioning error, and under
            # `auto` a repository run refuses one with the fix named.
            update["sandbox"] = SandboxConfig.model_validate(
                {
                    **sandbox.model_dump(),
                    "workspace_isolation": self.config.daemon.workspace_isolation,
                }
            )
        elif (
            item.kind == "code"
            and gh.repo is not None
            and self.config.workspace_for_repo(gh.repo) is None
        ):
            # A code run for a repository with no host checkout of it (the
            # home's clone failed, or never finished): its tree comes from
            # the repository's remote, or the run fails naming why. Under
            # `auto` it would otherwise start in an empty directory and
            # build the ask without the repository at all. `clone` is the
            # mode whose no-checkout path is exactly that remote clone.
            update["sandbox"] = SandboxConfig.model_validate(
                {**sandbox.model_dump(), "workspace_isolation": "clone"}
            )
        return self.config.model_copy(update=update)

    def _workspace_checkout(self, repo: str | None = None) -> Path | None:
        """``repo``'s configured workspace when it is the root of a git
        checkout (the only case isolation, and the fetch refresh, apply to).

        Resolved per repository (:meth:`Config.workspace_for_repo`), never
        from the daemon-wide path: with several repositories configured one
        ``[sandbox] workspace`` would otherwise stand in for every repo's
        tree (#526).
        """
        source = self.config.workspace_for_repo(repo)
        if source is None or hostgit.find_git() is None:
            return None
        source = source.resolve()
        return source if hostgit.repo_toplevel(source) == source else None

    def _ensure_workspace(self, repo: str | None) -> None:
        """Clone ``repo`` into the home's ``workspaces/<owner>/<name>`` the
        first time it is needed, when the operator pointed the daemon at no
        checkout of their own. Never fatal: without the checkout the run
        clones from the remote itself (:meth:`_item_config` pins that for a
        code run with no checkout). An empty directory there is cloned into;
        one with files in it that is not a checkout is left alone."""
        if repo is None:
            return
        target = self.config.default_workspace_for_repo(repo)
        if target is None or target.is_symlink() or hostgit.find_git() is None:
            return
        if self.config.workspace_for_repo(repo) is not None:
            # A checkout already resolves for this repository: the
            # operator's own, or the home's clone from an earlier run.
            return
        if target.exists():
            if not target.is_dir():
                return
            if any(target.iterdir()):
                # A directory with files in it that is not a checkout (a
                # leftover, or something the operator put there) is never
                # cloned over or removed. Only an empty one is initialized.
                self._notice(
                    "workspace.refresh_failed",
                    f"{target} exists but is not a git checkout of {repo}; move it "
                    f"aside so {repo} can be cloned there. Runs clone from the "
                    "remote meanwhile",
                    level="warning",
                    path=str(target),
                )
                return
        url = self.config.clone_url_for_repo(repo)
        token = self.github.provisioner.clone_token(repo) if self.github is not None else None
        started = time.monotonic()
        try:
            sha = hostgit.clone_workspace(
                url, target, token=token, clone_filter=self.config.sandbox.clone_filter
            )
        except ProvisionError as exc:
            self._notice(
                "workspace.refresh_failed",
                f"⚠ could not clone {repo} into {target}; runs will clone from the remote: {exc}",
                level="warning",
                path=str(target),
                error=str(exc),
                duration_s=round(time.monotonic() - started, 1),
            )
            return
        self._notice(
            "workspace.cloned",
            f"cloned {repo} into {target} (at {sha[:12]})",
            path=str(target),
            commit=sha,
            duration_s=round(time.monotonic() - started, 1),
        )

    def _refresh_workspace(self, repo: str | None = None) -> None:
        """Fetch + fast-forward *the claimed repo's* source checkout so the
        run's clone starts from current ``origin/<branch>`` (#255). Never
        fatal: a stale HEAD is still a run, a failed fetch (network blip,
        remote gone) is a warning in the chronology, not a failed issue.

        A repo the operator pointed at no checkout gets one cloned under the
        home first (:meth:`_ensure_workspace`); a checkout whose ``origin``
        names a different repository is refused rather than fast-forwarded
        (#526).

        An item with no repository (a chat workload, a legacy id) on a
        single-repo daemon refreshes that repository's checkout with that
        repository's credential: the checkout used to resolve from ``None``
        while the token did not, so a private forge was fetched anonymously
        and every chat ask posted a refresh failure. The first-use clone
        stays keyed on the item's own repository; a repo-less item never
        starts one.

        On a daemon with several repositories, or with every repository
        disabled, a repo-less item refreshes nothing: no sole enabled
        repository means no credential, and the checkout would otherwise
        fall back to the primary repository's, fetched anonymously -- a
        private forge answered with a username prompt and every chat ask or
        scheduled workload posted a refresh failure. Only a daemon that
        declares no repository at all falls through to the legacy
        daemon-wide workspace.

        Only a live *code* run holds the checkout still: a workload or tool
        run works from its own data directory, never from the checkout, so
        a code run admitted beside one still refreshes.
        """
        resolved = repo
        if resolved is None:
            default = self.config.default_repo()
            resolved = default.repo if default is not None else None
            if resolved is None and self.config.repo_list():
                log.info(
                    "workspace.refresh_skipped",
                    reason="the item names no repository and none is the sole enabled one",
                )
                return
        if resolved is not None and resolved in self._live_repos(code_only=True):
            # Another code run is cloning from this checkout right now:
            # moving it under that run's feet is not ours to do. The next
            # run started with the repository free refreshes it.
            log.info(
                "workspace.refresh_skipped",
                repo=resolved,
                reason="a run in flight is using this repository",
            )
            return
        self._ensure_workspace(repo)
        repo = resolved
        if not self.config.daemon.refresh_workspace:
            return
        if self.config.daemon.workspace_isolation == "in-place":
            # In-place runs mutate the checkout directly; fast-forwarding
            # under a tree the previous run edited is not ours to do.
            return
        source = self._workspace_checkout(repo)
        if source is None:
            log.info(
                "workspace.refresh_skipped",
                repo=repo,
                reason="no git checkout resolved for this repository",
            )
            return
        if repo is not None and hostgit.origin_matches_repo(source, repo) is False:
            actual = hostgit.normalise_repo_url(hostgit.origin_url(source))
            log.warning(
                "workspace.refresh_refused",
                repo=repo,
                path=str(source),
                origin=actual,
                reason=(
                    f"{source} is a checkout of {actual}, not {repo}; refusing to "
                    "refresh another repository's tree"
                ),
            )
            return
        log.debug("workspace.refresh_start", path=str(source))
        started = time.monotonic()
        try:
            token = (
                self.github.provisioner.clone_token(repo)
                if self.github is not None and repo is not None
                else None
            )
            result = hostgit.refresh_from_origin(
                source, token=token, credential_url=self.config.forge_web_url(repo)
            )
        except ProvisionError as exc:
            self._notice(
                "workspace.refresh_failed",
                f"⚠ workspace refresh failed; running from local HEAD: {exc}",
                level="warning",
                path=str(source),
                error=str(exc),
                duration_s=round(time.monotonic() - started, 1),
            )
            return
        if result.advanced:
            self._notice(
                "workspace.refreshed",
                f"refreshed workspace: {result.message}",
                path=str(source),
                detail=result.message,
                duration_s=round(time.monotonic() - started, 1),
            )
        else:
            log.info(
                "workspace.refresh_unchanged",
                path=str(source),
                detail=result.message,
                duration_s=round(time.monotonic() - started, 1),
            )

    def outcome_text(self, item: WorkItem) -> str:
        """The issue as the decomposer reads it: title, body, the
        discussion and the linked issues (#691), provenance. No lanes — the
        run has none to be told about.

        On real trackers the body is a one-liner and the substance is in
        the comments, so the source is asked for them (minus the loop's
        own) and for the issues they refer to. Title, body and provenance
        are always carried whole; the discussion block is what
        ``[budgets] outcome_max_chars`` cuts, with a note saying so. A
        source that cannot read the comments leaves the outcome with an
        explicit line saying the discussion is missing — the run goes on
        with the ask itself rather than waiting on a GitHub read.
        """
        if is_local_id(item.item_id):
            # A chat ask (#760), a schedule tick (#761) or a remote API ask:
            # the ask is the whole ask, and there is no issue discussion to
            # fetch.
            if is_chat_id(item.item_id):
                who = f"<@{item.requested_by}>" if item.requested_by else "an operator"
                origin = f"a chat ask by {who}"
            elif is_api_id(item.item_id):
                origin = "a request admitted through the remote API"
            else:
                name, due = parse_schedule_id(item.item_id)
                origin = f"the schedule `{name}`, due {due}"
            return "\n\n".join(
                [
                    _MARKER_RE.sub("", item.body).strip() or item.title.strip(),
                    f"---\nThis work item came from: {origin}.",
                ]
            )
        parts = [item.title.strip()]
        body = _MARKER_RE.sub("", item.body).strip()
        if body:
            parts.append(body)
        where = self._item_repo(item) or self.config.primary_repo
        origin = f"GitHub issue #{item.source_key} in {where}"
        if item.url:
            origin += f" ({item.url})"
        provenance = f"---\nThis work item came from: {origin}."
        context = self._issue_context_block(item)
        if context:
            # What the budget leaves once the block's own separator is paid for.
            room = self.config.budgets.outcome_max_chars - len(
                "\n\n".join([*parts, "", provenance])
            )
            parts.append(_fit_context(context, room, self.config.budgets.outcome_max_chars))
        parts.append(provenance)
        return "\n\n".join(parts)

    def _issue_context_block(self, item: WorkItem) -> str:
        """The rendered discussion and linked issues, "" when the source
        has none to offer, or the one-line note when it could not read them."""
        read = getattr(self.source, "issue_context", None)
        if read is None:
            return ""
        try:
            context: IssueContext = read(item, own=self._own_identity(item).identity)
        except Exception as exc:
            log.warning("run.issue_context_failed", item=item.item_id, exc_info=True)
            return (
                "(The issue's comments could not be read — "
                f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]} — so any "
                "discussion under it is not shown here; the title and body above are "
                "the whole ask as far as this run can see.)"
            )
        return _render_context(context)

    def _own_identity(self, item: WorkItem) -> LoopIdentity:
        """The loop's own GitHub identity, for leaving its comments out of
        the discussion; unknown when nothing can answer, and then only the
        marker-stamped comments are left out."""
        if self.github is None:
            return UNKNOWN_IDENTITY
        repo = self._item_repo(item) or self.config.primary_repo
        if repo is None:
            return UNKNOWN_IDENTITY
        try:
            return resolve_identity(
                self.github.ops(),
                repo,
                None,
                bot_login=self.github.provisioner.gh_bot_login(repo),
                configured_login=self.config.github.bot_login_for(repo),
            )
        except Exception as exc:
            self.github.note_failure(exc)
            log.warning("run.identity_lookup_failed", item=item.item_id, exc_info=True)
            return UNKNOWN_IDENTITY

    def _default_runner(
        self, item: WorkItem, item_config: Config, run_id: str, bus: EventBus, resume: bool
    ) -> RunResult:
        # _launch built the engine (so a cancel and the frontend see the one
        # that is actually running); use it rather than a second one.
        handle = self._live_run(run_id)
        assert handle is not None  # nosec B101 - registered before its thread starts
        engine = handle.engine
        if resume:
            return engine.resume(run_id, release_provider_hold=False)
        if item.recipe is not None:
            recipe = get_recipe(item.recipe)
            target = item.recipe_target or ""
            item_config = recipe.config(item_config, target)
            engine.config = item_config
            recipe.stage(item_config, run_id, target)
            # A `tool` run: the recipe's command and checks, no agent, the
            # result files to chat. The recipe just wrote the run's inputs,
            # and a tool run requires its mount by default.
            return engine.start(
                self.outcome_text(item),
                run_id=run_id,
                tasks=[recipe.task(item_config, target)],
                repo=item.repo,
                kind="tool",
            )
        # A restart by re-applied label continues the previous attempt's
        # pushed branch and PR where they are still usable (#600); the
        # engine confirms that with GitHub and falls back to a fresh start.
        prior = self.dstore.prior_attempt(item.item_id)
        return engine.start(
            self.outcome_text(item),
            run_id=run_id,
            warm=self._warm_run(run_id),
            repo=self._item_repo(item),
            prior_branch=prior.branch if prior else None,
            prior_pr=prior.pr_number if prior else None,
            # A workload item (#760) runs the operator persona under its
            # profile; a code item passes the defaults, as it always has.
            kind=item.kind,
            profile=item.profile,
            assignment=_item_assignment(item),
        )

    # -- reporting -----------------------------------------------------------------------

    def report_for(self, run_id: str) -> RunReport:
        """The report card of any run this daemon's store knows — what the
        settle path reads, for the concierge and other readers."""
        return self._report(run_id, None)

    def _exhaustion(self, run_id: str, result: RunResult | None) -> RunRecord | None:
        """The run record when the run ended by exhausting a fix-round
        budget — the engine marks ``runs.exhausted`` at that moment."""
        if result is None or result.state != "failed":
            return None
        try:
            record = self.store.get_run(run_id)
        except SbxloopError:
            return None
        return record if record.exhausted is not None else None

    def _settle_exhausted(
        self,
        item: WorkItem,
        run_id: str,
        record: RunRecord,
        report: RunReport,
        reason: str,
        now: float,
    ) -> TickOutcome:
        """The run stopped one fix round short (#523): its branch is green
        and its PR is open, so starting over — a fresh plan, a second PR —
        throws away hours of work that was one round from landing.

        First exhaustion: grant ``[landing] retry_rounds`` and schedule a
        resume of the *same* run after the retry backoff; the attempt count
        and the breaker are untouched (a scheduled continuation is not a
        failure). Second exhaustion, or ``retry_rounds = 0``: hand over —
        the item fails with the run pinned, the issue gets the failed
        label, and ``grant-rounds`` is the operator's way to continue it."""
        landing = self.config.landing
        rounds = landing.retry_rounds
        pr = report.pr
        pr_text = f" on PR {pr[1]}" if pr and pr[1] else ""
        if record.granted_rounds == 0 and rounds > 0:
            self.store.grant_rounds(run_id, rounds)
            self.dstore.finish_ledger(run_id, "exhausted", now)
            backoff = self.config.daemon.retry_backoff_s
            item_reason = (
                f"{reason}. Resuming this run{pr_text} with {rounds} more fix round(s) "
                f"after the retry backoff ({backoff:.0f}s)"
            )
            self.dstore.mark_exhausted(item.item_id, item_reason, now, not_before=now + backoff)
            attempts_left = max(0, self.config.daemon.max_attempts_per_item - item.attempts)
            self.source.report_retry(item, item_reason, attempts_left)
            self._notice(
                "run.exhausted",
                f"⏳ {item.item_id} exhausted its {record.exhausted} fix rounds ({reason})"
                f"{pr_text}; resuming the same run in {backoff / 60:.0f} min with {rounds} more "
                f"round(s) — `grant-rounds {run_id} N` resumes now, `abandon {item.item_id}` stops",
                level="warning",
                item=item.item_id,
                run=run_id,
                url=pr[1] if pr else None,
                reason=reason,
                budget=record.exhausted,
                granted_rounds=rounds,
                retry_backoff_s=backoff,
                pr=pr[0] if pr else None,
            )
            self._frontend_finished(item, report)
            return "retry"
        self._set_breaker(self._breaker_opened_at, self._consecutive_failures + 1)
        self.dstore.finish_ledger(run_id, "failed", now)
        why = (
            f"{reason} ({record.granted_rounds} already granted)"
            if record.granted_rounds
            else f"{reason} (retry_rounds = 0)"
        )
        self.dstore.mark_failed(item.item_id, why, now, requeue=False)
        self.source.report_abandoned(item, why)
        self._notice(
            "run.exhausted",
            f"❌ {item.item_id} exhausted its {record.exhausted} fix rounds again ({reason})"
            f"{pr_text}; handed over — `grant-rounds {run_id} N` continues the same PR, "
            f"`retry {item.item_id}` starts a fresh plan",
            level="error",
            item=item.item_id,
            run=run_id,
            url=pr[1] if pr else None,
            reason=reason,
            budget=record.exhausted,
            granted_rounds=record.granted_rounds,
            pr=pr[0] if pr else None,
            consecutive_failures=self._consecutive_failures,
            hint="the run used every fix round it was granted and the checks it was "
            "fixing are still not passing; the PR stands and nothing retries on its own "
            "— grant more rounds to continue it, or retry the item for a fresh plan",
        )
        self._frontend_finished(item, report)
        if self._consecutive_failures >= self.config.daemon.max_consecutive_failures:
            self._set_breaker(now, self._consecutive_failures)
            self._notice(
                "breaker.opened",
                f"🛑 circuit breaker opened after {self._consecutive_failures} consecutive "
                f"failures; pausing dispatch for {self.config.daemon.breaker_cooldown_s:.0f}s",
                level="error",
                consecutive_failures=self._consecutive_failures,
                cooldown_s=self.config.daemon.breaker_cooldown_s,
                hint=_BREAKER_HINT,
            )
        return "failed"

    def _report(self, run_id: str, result: RunResult | None) -> RunReport:
        record: RunRecord | None
        try:
            record = self.store.get_run(run_id)
            state = record.state
            tasks = self.store.get_tasks(run_id)
        except SbxloopError:
            record = None
            state = result.state if result is not None else "failed"
            tasks = result.tasks if result is not None else []
        done = sum(1 for t in tasks if t.state == "done")
        summary = f"{done}/{len(tasks)} tasks done" if tasks else "no tasks ran"
        pr: tuple[int, str] | None = None
        branch = None
        rounds = 0
        reason = result.reason if result is not None else None
        if record is not None:
            if record.pr_number is not None:
                pr = (record.pr_number, record.pr_url or "")
            branch = record.branch
            rounds = record.review_rounds + record.ci_rounds
            reason = reason or record.reason
        elif result is not None and result.pr_number is not None:
            pr = (result.pr_number, result.pr_url or "")
        workspace = (
            str(result.workspace)
            if result is not None and result.workspace
            else (str(record.workspace) if record is not None and record.workspace else None)
        )
        kind: RunKind = record.kind if record is not None else (result.kind if result else "code")
        outputs: tuple[TaskOutcome, ...] = ()
        closing: str | None = None
        if kind != "code":
            outputs = tuple(self._task_outcome(run_id, t) for t in tasks)
            closing = run_summary(kind, tasks, record.pr_title if record is not None else None)
        return RunReport(
            run_id,
            state,
            summary,
            pr=pr,
            branch=branch,
            rounds=rounds,
            reason=reason,
            workspace=workspace,
            kind=kind,
            outputs=outputs,
            summary=closing,
            published=tuple(record.published) if record is not None else (),
        )

    def _task_outcome(self, run_id: str, task: TaskRecord) -> TaskOutcome:
        """One task as the finish card shows it (#757): its output and the
        judge's last word, read from the judge's own phase row."""
        verdict: str | None = None
        try:
            raw = self.store.latest_phase_output(run_id, task.spec.id, "judge")
        except SbxloopError:
            raw = None
        if raw:
            try:
                row = json.loads(raw)
            except ValueError:
                row = {}
            if row.get("degraded"):
                verdict = "no usable verdict — failed closed"
            elif row.get("passed"):
                verdict = "passed"
            else:
                unmet = [str(u) for u in row.get("unmet") or []]
                verdict = "failed"
                if unmet:
                    verdict += f" — unmet: {unmet[0]}"
                    if len(unmet) > 1:
                        verdict += f" (+{len(unmet) - 1} more)"
        output = task.output
        return TaskOutcome(
            task.spec.id,
            task.spec.title,
            task.state,
            output.summary if output is not None else "",
            output.file_count if output is not None else 0,
            verdict,
        )

    # -- recovery ------------------------------------------------------------------------

    def recover(self) -> None:
        """Reconcile items left ``running`` by a previous process.

        Finished runs are settled here; an interrupted run is only *queued
        for resume* — the actual resume happens in :meth:`tick`, behind the
        breaker / daily cap / pause gate and the per-item resume budget.
        Recovery used to dispatch resumes directly, so a daemon restarting
        into a bad state (breaker open, cap spent, operator-paused) resumed
        anyway (#254).

        Finishes with :meth:`_reconcile_orphan_runs`, which closes any run
        row a dead process left non-terminal (#374).

        Opens by stamping this process's generation and settling the
        operations a previous generation left unfinished — from the
        evidence the domain kept, before anything here changes it."""
        self.generation = new_generation_id()
        self.dstore.set_value(GENERATION_KEY, self.generation)
        self.dstore.set_value(GENERATION_STARTED_KEY, repr(self.clock()))
        self._activate_repositories()
        with self._holds_lock:
            restored = self.dstore.holds()
            self._holds = {h.name for h in restored}
        if restored:
            self._notice(
                "daemon.holds_restored",
                "still paused by "
                + ", ".join(
                    h.name + (f" ({h.owner_display})" if h.owner_display else "") for h in restored
                )
                + " — the holds survived the restart; `resume --hold <name>` releases one, "
                "`resume --all` every one",
                holds=[h.name for h in restored],
            )
        self._settle_half_claims()
        self._reconcile_gates()
        self._reconcile_review_holds()
        reconcile_operations(self, generation=self.generation, now=self.clock())
        for item in self.dstore.running_items():
            now = self.clock()
            if item.run_id is None:
                self.dstore.mark_requeued_unstarted(item.item_id, now)
                self._notice(
                    "recovery.requeued",
                    f"recovery: {item.item_id} re-queued (claimed, never started)",
                    item=item.item_id,
                    reason="claimed, never started",
                )
                continue
            try:
                record = self.store.get_run(item.run_id)
            except SbxloopError:
                self.dstore.mark_requeued_unstarted(item.item_id, now)
                log.warning(
                    "recovery.requeued",
                    item=item.item_id,
                    run=item.run_id,
                    reason="run record missing; starting over",
                )
                continue
            if record.state in (
                "merged",
                "blocked",
                "completed",
                "gated",
                "awaiting_review",
                "held",
            ):
                self._notice(
                    "recovery.settling",
                    f"recovery: {item.run_id} had ended {record.state}; settling {item.item_id}",
                    item=item.item_id,
                    run=item.run_id,
                    state=record.state,
                )
                self._settle(item, item.run_id, self._result_from_record(item.run_id), None)
            elif record.state in TERMINAL_RUN_STATES:
                self._notice(
                    "recovery.settling",
                    f"recovery: {item.run_id} ended {record.state}; applying failure path",
                    item=item.item_id,
                    run=item.run_id,
                    state=record.state,
                )
                self._settle(item, item.run_id, None, StateError(f"run ended {record.state}"))
            elif record.state in RESUMABLE_RUN_STATES:
                last = self.store.last_event_ts(item.run_id)
                self._notice(
                    "recovery.resume_pending",
                    f"recovery: {item.run_id} for {item.item_id} queued for resume "
                    f"(last activity {self.clock() - last:.0f}s ago)"
                    if last
                    else f"recovery: {item.run_id} for {item.item_id} queued for resume",
                    item=item.item_id,
                    run=item.run_id,
                    state=record.state,
                    idle_s=round(self.clock() - last) if last else None,
                )
                self.dstore.mark_resume_pending(item.item_id, now)
            else:
                self.dstore.mark_requeued_unstarted(item.item_id, now)
                log.warning(
                    "recovery.requeued",
                    item=item.item_id,
                    run=item.run_id,
                    state=record.state,
                    reason="run state neither terminal nor resumable; starting over",
                )
        self._settle_offline_overrides()
        self._reconcile_orphan_runs()

    def _settle_half_claims(self) -> None:
        """Rows whose claim was started but never completed (#530): the
        previous process died between posting the claim comment and
        persisting the claim. The source says whether the comment landed —
        then the claim is finished and the item dispatches normally; else
        the token is cleared and the next tick claims from scratch. Either
        way the issue is never left wearing a claim nobody will act on."""
        for item in self.dstore.half_claimed():
            now = self.clock()
            if self.source.settle_claim(item):
                self.dstore.mark_claimed(item.item_id, now)
                outcome = "claimed"
            else:
                self.dstore.clear_claim(item.item_id, now)
                outcome = "cleared"
            self._notice(
                "recovery.claim_settled",
                f"recovery: {item.item_id} was half-claimed by the previous process; "
                + (
                    "its claim comment is on the issue, so the claim is finished"
                    if outcome == "claimed"
                    else "nothing reached the issue, so it will be claimed again"
                ),
                item=item.item_id,
                outcome=outcome,
                token=item.claim_token,
            )

    def _reconcile_orphan_runs(self) -> None:
        """Force every *orphaned* non-terminal run to a terminal state (#374).

        The run row is only ever written by the in-process run loop, so a
        cancelled item or a dead process left phantom ``running`` /
        ``decomposing`` runs behind: ``list_runs`` disagreed with
        ``!sbx status`` and anything counting active runs was misled.

        Two kinds of run are deliberately left alone: the run genuinely
        executing in this process, and one queued for resume (item
        ``queued`` with the run still pinned — the ``mark_resume_pending``
        path above), which tick will pick up. Everything else is closed as
        ``cancelled`` (its item was cancelled) or ``failed`` (orphaned).
        Chronology is only ever appended to.
        """
        live = {handle.run_id for handle in self.runs}
        for record in self.store.non_terminal_runs():
            if record.run_id in live:
                continue
            item_id = self.dstore.item_for_run(record.run_id)
            item = self.dstore.get(item_id) if item_id else None
            if item is not None and item.state == "queued" and item.run_id == record.run_id:
                continue  # pending resume: tick owns it
            self._reconcile_run_record(record)

    def _reconcile_run_record(
        self, record: RunRecord, *, reason_override: str | None = None
    ) -> bool:
        """Close one non-terminal run, appending (never mutating) chronology.

        Shared by startup reconciliation and the tick-time staleness sweep.
        A cancelled work item wins over ``reason_override`` so operator
        attribution is preserved in the run's reason.
        """
        item_id = self.dstore.item_for_run(record.run_id)
        item = self.dstore.get(item_id) if item_id else None
        if item is not None and item.state == "cancelled":
            state: RunState = "cancelled"
            reason = "work item cancelled"
            if item.last_error:
                reason = f"work item cancelled: {item.last_error}"
        else:
            state = "failed"
            reason = reason_override or "orphaned: daemon restarted while run was in flight"
        try:
            self.store.reconcile_run(record.run_id, state, reason)
            self.store.append_event(
                Event.now(
                    "run.reconciled",
                    record.run_id,
                    state=state,
                    reason=reason,
                    previous_state=record.state,
                    item=item_id,
                )
            )
        except (SbxloopError, StateError) as exc:
            log.warning("recovery.run_reconcile_failed", run=record.run_id, error=str(exc))
            return False
        self._notice(
            "recovery.run_reconciled",
            f"recovery: orphaned run {record.run_id} {record.state} -> {state} ({reason})",
            run=record.run_id,
            item=item_id,
            state=state,
            previous=record.state,
            level="warning",
        )
        return True

    def _reconcile_stale_runs(self, now: float) -> None:
        """Liveness safety net (#374): with no run executing in this process,
        close non-terminal runs that have shown no activity for
        ``[daemon] run_stale_after_s``. Disabled when the threshold is 0."""
        threshold = self.config.daemon.run_stale_after_s
        if threshold <= 0:
            return
        with self._current_lock:
            if self._runs:
                return  # a run is genuinely in flight; nothing here is stale
        for record in self.store.non_terminal_runs():
            last_activity = max(record.updated_at, self.store.last_event_ts(record.run_id) or 0.0)
            idle = now - last_activity
            if idle <= threshold:
                continue
            item_id = self.dstore.item_for_run(record.run_id)
            item = self.dstore.get(item_id) if item_id else None
            if item is not None and item.state == "queued" and item.run_id == record.run_id:
                continue  # pending resume: tick owns it
            self._reconcile_run_record(
                record, reason_override=f"orphaned: stale, no activity for {int(idle)}s"
            )

    def _settle_offline_overrides(self) -> None:
        """`sbxloop daemon abandon|requeue` while no daemon is running can
        only flip the row (field scenario of #229: the item was left
        running/pinned after a clean shutdown). The source was never told,
        the run's ledger row is still open (or ``interrupted``) and its
        microVMs still exist. Finish that work here so the abandon reaches
        the issue exactly once, on the next daemon start. The source report
        itself is the row's ``pending_report`` debt, paid by
        :meth:`_deliver_pending_reports` right after (and by every tick —
        an abandoned-while-queued item has no run to find here)."""
        for run_id, item_id in self.dstore.unsettled_runs():
            item = self.dstore.get(item_id)
            if item is None:
                continue
            now = self.clock()
            if item.state == "failed" and item.run_id == run_id:
                self._close_dead_run(run_id, "abandoned", now, repo=item.repo)
                self._notice(
                    "recovery.offline_abandon",
                    f"recovery: {item_id} abandoned offline; run {run_id} closed",
                    item=item_id,
                    run=run_id,
                )
            elif item.state == "queued" and item.run_id != run_id:
                # Requeued (unpinned) offline: the run is dead and will not be
                # resumed — close its ledger and drop its sandboxes.
                self._close_dead_run(run_id, "requeued", now, repo=item.repo)
                self._notice(
                    "recovery.offline_requeue",
                    f"recovery: {item_id} requeued offline; run {run_id} closed",
                    item=item_id,
                    run=run_id,
                )
            # A queued item still pinned to this run is a pending resume; a
            # running one was reconciled above.
        self._deliver_pending_reports()

    def _remove_stale_run_sandboxes(self, run_id: str, *, repo: str | None = None) -> None:
        """A dead process leaves the run's microVMs — and their secret
        registrations — behind. Both must go before resume re-provisions
        under the same names: a lingering secret cannot be replaced, so the
        agent would come up holding the proxy sentinel and the Copilot SDK
        401s (field failure rgn9ccjam)."""
        if self.sbx is None:
            return
        roles: tuple[SandboxRole, ...] = ("agent", "github")
        # The service sandbox (#765) exists for a run granted credentials
        # — the run row says — or for a repo with a credentialed registry
        # (#766); the run row does not say which repo, so any repo
        # configured with one is reason to sweep (a missing sandbox is the
        # common, tolerated case below).
        try:
            if self.store.get_run(run_id).credentials or self._any_credentialed_registries():
                roles += ("service",)
        except StateError:
            pass
        vcs_kind = self.config.vcs_kind_for(repo)
        for role in roles:
            for name in sandbox_name_candidates(
                run_id, role, vcs_kind=vcs_kind, home=self.config.paths
            ):
                try:
                    remove_run_sandbox(self.sbx, name, role, self.config)
                    self._notice(
                        "recovery.stale_sandbox_removed",
                        f"recovery: removed stale sandbox {name} (and its secrets)",
                        run=run_id,
                        sandbox=name,
                        role=role,
                    )
                except SbxError:
                    # No such sandbox — the common case — but a secret may
                    # still linger from a rollback race; clearing it is cheap.
                    log.debug("recovery.no_stale_sandbox", run=run_id, sandbox=name, role=role)
                    remove_run_sandbox_secrets(self.sbx, name, role, self.config)

    def _any_credentialed_registries(self) -> bool:
        """Whether any repo this daemon runs for fetches through a service
        sandbox (#766)."""
        repos: list[str | None] = [r.repo for r in self.config.vcs.repos] or [
            self.config.primary_repo
        ]
        return any(self.config.credentialed_registries_for(repo) for repo in repos)

    def _result_from_record(self, run_id: str) -> RunResult:
        record = self.store.get_run(run_id)
        return RunResult(
            run_id=run_id,
            state=record.state,
            tasks=self.store.get_tasks(run_id),
            workspace=record.workspace,
            mounted=record.mounted,
            pr_number=record.pr_number,
            pr_url=record.pr_url,
            reason=record.reason,
            kind=record.kind,
            summary=run_summary(record.kind, self.store.get_tasks(run_id), record.pr_title),
            published=list(record.published),
        )

    # -- helpers ------------------------------------------------------------------------

    def _notice(
        self,
        kind: NoticeKind,
        text: str,
        *,
        item: str | None = None,
        run: str | None = None,
        url: str | None = None,
        level: NoticeLevel = "info",
        mention_ids: Sequence[str] = (),
        **fields: Any,
    ) -> None:
        """Narrate to the humans (Discord) *and* the journal: ``text`` is the
        prose the frontend shows, routed by ``item``/``run``; ``kind`` and
        ``fields`` are the structured record the log keeps (``level`` picks
        its severity). ``mention_ids`` are the chat users the frontend
        should address by name (#675)."""
        getattr(log, level)(
            kind,
            text=text,
            item=item,
            run=run,
            **({"mentions": list(mention_ids)} if mention_ids else {}),
            **fields,
        )
        if self.frontend is not None:
            try:
                self.frontend.daemon_notice(
                    DaemonNotice(
                        kind,
                        text,
                        item_id=item,
                        run_id=run,
                        url=url,
                        level=level,
                        mention_ids=tuple(mention_ids),
                    )
                )
            except Exception:
                log.warning("frontend.daemon_notice_failed", notice=kind, exc_info=True)

    def _frontend_finished(self, item: WorkItem, report: RunReport) -> None:
        if self.frontend is not None:
            try:
                self.frontend.run_finished(item, report)
            except Exception:
                log.warning(
                    "frontend.run_finished_failed",
                    item=item.item_id,
                    run=report.run_id,
                    exc_info=True,
                )
