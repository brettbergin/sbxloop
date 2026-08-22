"""LoopEngine: drives DECOMPOSE → (PLAN → EXECUTE → SCRUTINIZE → VERIFY →
VALIDATE)* under budgets, with SQLite checkpointing after every transition.

Failure semantics:

- Budget exhaustion (revisions/replans) fails the *task*; dependents are
  skipped and the run continues, finishing ``failed`` if any task failed.
  One exception: revisions exhausted by *verify-command* failures spend a
  replan first when budget remains — the executor cannot edit verify
  commands, so only a fresh plan can unstick a broken check. The faster
  route out (#231): when the scrutinizer passes the work after a verify
  failure and flags the *check itself* as wrong (``verify_suspect``), the
  replan is spent immediately instead of after the revisions burn.
- Infrastructure errors (worker/sbx crashes) propagate after state is
  persisted — equivalent to a kill. ``resume()`` re-provisions a fresh
  sandbox pair (sandboxes are cattle; the workspace and SQLite state
  persist on the host) and continues from the last committed transition:
  a phase whose result was never committed re-runs from its start.
  Resume rehydrates the run's persisted config and pins the workspace
  from the runs table, so on-disk config edits (or a different cwd)
  cannot silently change the run's rules or relocate its workspace;
  drift is surfaced as a ``run.config_drift`` event.
"""

from __future__ import annotations

import json
import queue
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import ValidationError

from sbxloop.config import Config, GithubConfig, _flatten, load_config, load_dotenv_file
from sbxloop.deliver import deliver_workspace, ensure_repository
from sbxloop.engine.model import (
    RESUMABLE_RUN_STATES,
    TERMINAL_RUN_STATES,
    RunResult,
    RunState,
    SteerVerdict,
    TaskRecord,
    TaskState,
    Verdict,
    artifacts_dir,
    scan_artifacts,
)
from sbxloop.engine.phases import VERIFY_FAILURE_PREFIX, CriticOutcome, PhaseRunner, clip
from sbxloop.engine.store import StateStore
from sbxloop.errors import (
    BudgetExceededError,
    DeliveryError,
    RunCancelledError,
    SbxError,
    SbxloopError,
    StateError,
    WorkerError,
)
from sbxloop.events import EventBus, Hook, HostEventTypes
from sbxloop.gc import workspace_pruned
from sbxloop.gh.ops import GithubOps, PrRef
from sbxloop.gh.reporter import GithubReporterHook
from sbxloop.ids import new_message_id, new_run_id
from sbxloop.log import get_logger
from sbxloop.policy import EgressGranter
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.pair import SandboxPair
from sbxloop.sbx.provision import Provisioner, sandbox_name
from sbxloop.sbx.prune import remove_run_sandbox_secrets
from sbxloop.sbx.sandbox import SBXLOOP_DIR, Sandbox
from sbxloop.worker.client import WorkerClient

log = get_logger(__name__)


class ChatMessage(NamedTuple):
    """One queued interactive chat message, waiting for a phase boundary."""

    message_id: str
    text: str


class _GithubOnly:
    """A lone github-ops sandbox (``sbxloop deliver``) and its worker client.

    Not a :class:`SandboxPair` — there is no agent half — but the teardown
    is the pair's: stop, rm, and unregister the name-scoped secret, since
    ``sbx rm`` leaves the registration behind and it would poison the next
    provision under the same run name (see ``remove_run_sandbox_secrets``).
    """

    def __init__(self, sandbox: Sandbox, client: WorkerClient, *, keep: bool = False) -> None:
        self.sandbox = sandbox
        self.client = client
        self.keep = keep

    def close(self) -> None:
        if self.keep:
            return
        for step in (
            self.sandbox.stop,
            self.sandbox.rm,
            partial(remove_run_sandbox_secrets, self.sandbox.cli, self.sandbox.name, "github"),
        ):
            try:
                step()
            except Exception:
                log.warning("github_ops.teardown_failed", sandbox=self.sandbox.name, exc_info=True)


class LoopEngine:
    def __init__(
        self,
        config: Config | None = None,
        *,
        store: StateStore | None = None,
        bus: EventBus | None = None,
        hooks: Sequence[Hook] = (),
        sbx: SbxCLI | None = None,
        worker_python: str | None = None,
        install_workers: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Library parity with the CLI: a ./.env supplies tokens/settings even
        # when the caller passes a prebuilt Config (real env vars still win).
        load_dotenv_file()
        self.config = config or load_config()
        self.store = store or StateStore(self.config.state_dir / "state.db")
        self.bus = bus or EventBus()
        self.sbx = sbx or SbxCLI(app_name=self.config.app_name or None)
        self.worker_python = (
            worker_python if worker_python is not None else (self.config.worker_python)
        )
        self.install_workers = (
            install_workers if install_workers is not None else self.config.install_workers
        )
        # Which collaborators were derived from config (vs passed explicitly):
        # resume() re-derives exactly these after rehydrating the run's
        # persisted config, and leaves caller-supplied ones alone.
        self._sbx_from_config = sbx is None
        self._worker_python_from_config = worker_python is None
        self._install_workers_from_config = install_workers is None
        self.clock = clock
        # In-process cancellation (Ctrl-C in the TUI): checked at the same
        # phase boundaries as the store's cancelled state, but leaves the
        # persisted run state alone so the run stays resumable.
        self._cancel_event = threading.Event()
        # Latest sandbox.resources sample per sandbox role, fed by the bus;
        # consulted for the disk guardrail and the harvest-truncation note.
        self._last_resources: dict[str, dict[str, object]] = {}
        # Interactive chat mailbox: messages posted from any thread (the CLI
        # chat form) queue here and are absorbed at phase boundaries — the
        # same boundaries cancellation uses. All bus/store activity for a
        # message happens on the engine thread when it is drained.
        self._chat_queue: queue.SimpleQueue[ChatMessage] = queue.SimpleQueue()
        self._steer_attempts = 0
        # Held while one task lane drains the chat mailbox. Taken
        # non-blocking: with several lanes in flight every one of them
        # reaches a phase boundary, and the queue only needs draining once —
        # a lane that finds the lock taken carries on rather than piling up
        # behind an LLM round trip it does not need to wait for.
        self._chat_lock = threading.Lock()
        # Serialises the operations that act on the shared agent sandbox
        # rather than on one task's own state: egress grants (which rewrite
        # the sandbox's network policy) and artifact harvests (which copy the
        # whole workspace out). Concurrent lanes would otherwise interleave
        # inside them.
        self._sandbox_lock = threading.RLock()
        for hook in hooks:
            self.bus.attach_hook(hook)
        self.bus.subscribe(self._persist_event)
        self.bus.subscribe(self._track_resources)

    def _persist_event(self, event: object) -> None:
        from sbxloop_worker.protocol import Event

        assert isinstance(event, Event)
        self.store.append_event(event)

    def _track_resources(self, event: object) -> None:
        from sbxloop_worker.protocol import Event, EventTypes

        assert isinstance(event, Event)
        if event.type == EventTypes.SANDBOX_RESOURCES:
            role = str(event.data.get("role") or "agent")
            self._last_resources[role] = dict(event.data)

    # -- public API --------------------------------------------------------

    def start(self, outcome: str, *, run_id: str | None = None) -> RunResult:
        run_id = run_id or new_run_id()
        self.store.create_run(run_id, outcome, self.config.model_dump_json())
        self.bus.emit(HostEventTypes.RUN_START, run_id, outcome=outcome)
        return self._drive(run_id, outcome)

    def resume(self, run_id: str) -> RunResult:
        run = self.store.get_run(run_id)
        if run.state not in RESUMABLE_RUN_STATES:
            raise StateError(f"run {run_id} is {run.state}; only unfinished runs can resume")
        self._refuse_if_pruned(run_id)
        if run.state in TERMINAL_RUN_STATES:
            # A failed run is both terminal (so gc may take it) and resumable.
            # gc claims a directory only while the run is terminal, in one
            # write transaction with its marker; leaving the terminal set
            # BEFORE touching the workspace — and re-checking after — means
            # whichever of the two committed first wins, and a sweep in
            # another process can never pull the workspace out from under a
            # resume that already passed the guard.
            self.store.set_run_state(run_id, "provisioning")
            try:
                self._refuse_if_pruned(run_id)
            except StateError:
                self.store.set_run_state(run_id, run.state)
                raise
        self._rehydrate_config(run_id)
        self.bus.emit(HostEventTypes.RUN_START, run_id, outcome=run.outcome, resumed=True)
        return self._drive(run_id, run.outcome, workspace=run.workspace)

    def _refuse_if_pruned(self, run_id: str) -> None:
        if workspace_pruned(self.store, run_id):
            # The workspace pin would be re-created empty and the agent's
            # prior work is gone; say so rather than resuming into nothing.
            raise StateError(
                f"run {run_id}: its workspace was removed by gc (see `sbxloop logs {run_id} "
                "--type daemon.gc`); it cannot be resumed — start a new run"
            )

    def cancel(self, run_id: str) -> None:
        run = self.store.get_run(run_id)  # raises for unknown runs
        if run.state in TERMINAL_RUN_STATES:
            # Rewriting a finished run to cancelled would corrupt history
            # (and `status` output); only in-flight runs are cancellable.
            raise StateError(f"run {run_id} is already {run.state}; nothing to cancel")
        self.store.set_run_state(run_id, "cancelled")

    def deliver(
        self,
        run_id: str,
        *,
        github_overrides: dict[str, Any] | None = None,
        report: bool | None = None,
    ) -> PrRef:
        """Deliver (or re-deliver) a completed run's artifacts as a PR.

        Delivery at run end is a one-shot side effect: when it fails, the
        work is done, verified, and stranded in the workspace, and
        ``resume`` refuses completed runs (field failure rgwp5z40x, #223).
        This is the retry path: a github-ops sandbox alone (no agent, no
        Copilot token), the run's persisted config (same pinning
        discipline as resume) with any explicit ``github_overrides`` on
        top — a run that never had ``[github].repo`` can still be
        delivered by naming one — and the same ``run.deliver`` events, so
        ``logs`` and the finish summary see the outcome. Unlike the
        end-of-run hook, failure raises: the caller asked for exactly this.

        ``report`` (None → the run's ``[github].report``) refreshes the
        tracking issue with the PR link once delivery succeeds.
        """
        run = self.store.get_run(run_id)
        if run.state != "completed":
            raise StateError(f"run {run_id} is {run.state}; only completed runs can be delivered")
        self._rehydrate_config(run_id)
        github_cfg = self.config.github
        if github_overrides:
            # Validate, don't model_copy: an ill-formed --repo must fail here.
            github_cfg = GithubConfig.model_validate(
                {**github_cfg.model_dump(), **github_overrides}
            )
            self.config = self.config.model_copy(update={"github": github_cfg})
        repo = github_cfg.repo
        if not repo:
            raise StateError(
                f"run {run_id} has no delivery repository: its config has no "
                "[github].repo — pass --repo owner/name"
            )
        source = artifacts_dir(run, self.config.state_dir)
        if source is None or not source.is_dir():
            raise DeliveryError(
                f"run {run_id} has no artifacts directory to deliver "
                f"({source or 'no workspace recorded'})"
            )
        report_wanted = github_cfg.report if report is None else report
        assert run.workspace is not None
        try:
            # Provisioning is part of the retry attempt: a missing token or a
            # sandbox/worker failure must land in `logs` as a failed
            # `run.deliver` too, not vanish into a raised exception.
            sandbox = self._provision_github_only(run_id, run.workspace)
        except SbxloopError as exc:
            self.bus.emit(HostEventTypes.RUN_DELIVER, run_id, repo=repo, error=str(exc))
            raise
        try:
            ops = GithubOps(sandbox.client, run_id)
            try:
                created = ensure_repository(
                    ops, repo, create=github_cfg.create_repo, public=github_cfg.create_public
                )
                if created:
                    self.bus.emit(HostEventTypes.RUN_DELIVER, run_id, repo=repo, created=True)
                pr = deliver_workspace(
                    ops,
                    repo,
                    run_id=run_id,
                    outcome=run.outcome,
                    source_dir=source,
                    base=github_cfg.deliver_base,
                    draft=github_cfg.deliver_draft,
                    exclude=self.config.artifacts.exclude,
                )
            except SbxloopError as exc:
                self.bus.emit(HostEventTypes.RUN_DELIVER, run_id, repo=repo, error=str(exc))
                raise
            self.bus.emit(HostEventTypes.RUN_DELIVER, run_id, repo=repo, pr=pr.number, url=pr.url)
            if report_wanted:
                hook = GithubReporterHook(ops, repo)
                hook.open_run(run_id, run.outcome)
                hook.note_delivery(run_id, pr)
                if hook.issue is not None:
                    self.bus.emit(
                        HostEventTypes.RUN_REPORT,
                        run_id,
                        repo=repo,
                        issue=hook.issue.number,
                        url=hook.issue.url,
                    )
        finally:
            sandbox.close()
        return pr

    def _provision_github_only(self, run_id: str, workspace: Path) -> _GithubOnly:
        """One github-role sandbox under the run's github sandbox name (so
        `status`/`shell --role github`/`sandbox prune` all recognize it),
        worker installed, torn down by the returned handle's ``close``."""
        clients: list[WorkerClient] = []

        def install(created: Sandbox, _role: str) -> None:
            # Inside ensure_github_only's try: a failed install rolls the
            # sandbox and its registered secret back, as for the pair.
            client = WorkerClient(
                created,
                self.bus,
                transport=self.config.worker_transport,
                python=self.worker_python,
                role="github",
                limits=self.config.limits,
            )
            if self.install_workers:
                client.install(extras="", expect_prebaked=bool(self.config.sandbox.template))
            clients.append(client)

        provisioner = Provisioner(self.sbx, self.config, self.bus)
        sandbox = provisioner.ensure_github_only(
            sandbox_name(run_id, "github"), workspace, post_create=install, run_id=run_id
        )
        if self.config.keep_sandboxes:
            # Same marker a kept run gets, so `sandbox prune` respects it.
            self.store.set_run_kept(run_id, "manual")
        return _GithubOnly(sandbox, clients[0], keep=self.config.keep_sandboxes)

    def request_cancel(self) -> None:
        """Ask a running engine (from another thread) to stop at the next
        phase boundary. In-process only: unlike ``cancel`` it does not touch
        the persisted run state, so the interrupted run remains resumable."""
        self._cancel_event.set()

    def post_user_message(self, text: str) -> str:
        """Queue an interactive chat message for the run this engine is
        driving. Thread-safe; returns the message id. The agent pauses at
        the next phase boundary, answers over a read-only STEER session, and
        applies any course change the reply calls for.
        """
        message = ChatMessage(new_message_id(), text)
        self._chat_queue.put(message)
        return message.message_id

    # -- resume config rehydration ------------------------------------------

    def _rehydrate_config(self, run_id: str) -> None:
        """Adopt the config persisted when the run was created, so a resumed
        run keeps its original rules (budgets, model, github toggles,
        workspace) even if the on-disk config changed — or the resume happens
        from a different directory — in between.

        Tokens still come from the current environment (they are never
        persisted), and ``state_dir`` stays the one that located the run: the
        store is already open there. The debug/cleanup toggles
        (``keep_sandboxes``, ``keep_on_failure``) also stay resume-time
        choices — they are operator intent about THIS attempt, not run
        identity, and flipping keep on to debug a crashing run must work.
        Drift from the config this engine was built with is reported via a
        ``run.config_drift`` event, never applied silently.
        """
        raw = self.store.get_run_config(run_id)
        try:
            legacy = not json.loads(raw)
        except ValueError:
            legacy = False
        if legacy:
            # Row predates config persistence; current config is all we have.
            return
        try:
            stored = Config.model_validate_json(raw)
        except ValidationError as exc:
            message = (
                "persisted run config no longer validates (config schema "
                "changed since the run started?); resuming with the current "
                f"config instead: {exc}"
            )
            log.warning("run.config_invalid_on_resume", run=run_id, detail=message)
            self.bus.emit(HostEventTypes.RUN_CONFIG_DRIFT, run_id, message=message)
            return
        stored = stored.model_copy(
            update={
                "state_dir": self.config.state_dir,
                "keep_sandboxes": self.config.keep_sandboxes,
                "keep_on_failure": self.config.keep_on_failure,
            }
        )
        drift = self._config_drift(stored, self.config)
        if drift:
            message = (
                "resuming with the run's original config; the current config "
                "differs: " + "; ".join(drift)
            )
            log.warning("run.config_drift", run=run_id, drift=drift)
            self.bus.emit(HostEventTypes.RUN_CONFIG_DRIFT, run_id, message=message)
        self.config = stored
        if self._worker_python_from_config:
            self.worker_python = stored.worker_python
        if self._install_workers_from_config:
            self.install_workers = stored.install_workers
        if self._sbx_from_config:
            self.sbx = SbxCLI(app_name=stored.app_name or None)

    @staticmethod
    def _config_drift(stored: Config, current: Config) -> list[str]:
        """Dotted keys where the run's persisted config and the config this
        engine was built with disagree, with both values."""
        stored_flat = _flatten(stored.model_dump(mode="json"))
        current_flat = _flatten(current.model_dump(mode="json"))
        return [
            f"{key} (run: {stored_flat.get(key)!r}, current: {current_flat.get(key)!r})"
            for key in sorted(stored_flat.keys() | current_flat.keys())
            if stored_flat.get(key) != current_flat.get(key)
        ]

    # -- run driver --------------------------------------------------------

    def _drive(self, run_id: str, outcome: str, *, workspace: Path | None = None) -> RunResult:
        deadline = self.clock() + self.config.budgets.max_wall_clock_s
        self._set_run_state(run_id, "provisioning")
        provisioner = Provisioner(self.sbx, self.config, self.bus)
        # A resumed run's workspace is pinned from the runs table — never
        # recomputed from config, which would silently relocate it (#60).
        pair = provisioner.ensure_pair(run_id, workspace)
        assert pair.workspace is not None
        if workspace is not None and pair.workspace != workspace:
            raise StateError(
                f"run {run_id} workspace mismatch: the run recorded {workspace} "
                f"but provisioning produced {pair.workspace}; refusing to "
                "continue in a relocated workspace"
            )
        self.store.set_run_workspace(run_id, pair.workspace, pair.mounted)
        if pair.keep:
            # keep_sandboxes: mark up front so `sandbox prune` respects it.
            self.store.set_run_kept(run_id, "manual")
        try:
            with pair:
                try:
                    agent = WorkerClient(
                        pair.agent,
                        self.bus,
                        transport=self.config.worker_transport,
                        python=self.worker_python,
                        role="agent",
                        limits=self.config.limits,
                    )
                    github = (
                        WorkerClient(
                            pair.github,
                            self.bus,
                            transport=self.config.worker_transport,
                            python=self.worker_python,
                            role="github",
                            limits=self.config.limits,
                        )
                        if pair.github is not None
                        else None
                    )
                    if self.install_workers:
                        self._install_workers(run_id, pair, agent, github)
                    self._ensure_delivery_repo(run_id, github)
                    reporter, detach = self._attach_reporter(github, run_id, outcome)
                    try:
                        phases = PhaseRunner(
                            agent,
                            self.config,
                            run_id,
                            outcome,
                            workdir=pair.agent_workdir,
                            workspace=pair.workspace,
                        )
                        # Replay persisted chat guidance (steer_run verdicts)
                        # so a resumed run keeps the direction the user set.
                        for guidance in self.store.get_run_guidance(run_id):
                            phases.add_guidance(guidance)
                        granter = EgressGranter(
                            self.sbx, self.config, self.bus, run_id, pair.agent.name
                        )
                        state = self._run_phases(run_id, phases, deadline, pair, granter)
                        # Summary must post while the github sandbox is alive;
                        # on an infra exception the run is resumable and the
                        # resumed run reopens the same tracking issue.
                        if reporter is not None:
                            reporter.close_run(run_id, state)
                    finally:
                        detach()
                        # Harvest even when a phase raised: the sandbox is still
                        # alive here, and partial artifacts beat none.
                        self._harvest(run_id, pair)
                        self._report_artifacts(run_id, pair)
                except SbxloopError:
                    # Infra failures (install, worker, sbx) are exactly what
                    # gets diagnosed in-sandbox; decide keep before pair exit.
                    self._keep_on_failure(run_id, pair)
                    raise
                if state == "completed":
                    self._deliver(run_id, outcome, pair, github)
                else:
                    self._keep_on_failure(run_id, pair)
        except SbxloopError:
            # State is already persisted; the exception is the kill signal.
            raise
        self._set_run_state(run_id, state)
        tasks = self.store.get_tasks(run_id)
        self.bus.emit(HostEventTypes.RUN_END, run_id, state=state)
        return RunResult(
            run_id=run_id,
            state=state,
            tasks=tasks,
            workspace=pair.workspace,
            mounted=pair.mounted,
            kept_sandboxes=self._pair_names(pair) if pair.keep else [],
        )

    def _install_workers(
        self,
        run_id: str,
        pair: SandboxPair,
        agent: WorkerClient,
        github: WorkerClient | None,
    ) -> None:
        """Install the worker into both sandboxes, concurrently when the pair
        exists — the installs share nothing in-sandbox, and each is seconds
        of exec round-trips that would otherwise stack serially (#127).

        A configured template is expected to be prebaked (`sbxloop bake`):
        install() probes it and skips the ladder on success, falling back
        when stale. ensure_dev_tools is agent-only — the agent builds
        projects in its VM, so it gets the `[sandbox] languages` toolchains;
        the github sandbox only runs API ops. Both installs always run to
        completion before any failure
        propagates, so an error never unwinds into pair teardown while the
        other install is still mid-exec.
        """
        prebaked_expected = bool(self.config.sandbox.template)
        installs: list[Callable[[], None]] = [
            partial(
                agent.install,
                extras="copilot",
                ensure_dev_tools=True,
                languages=self.config.sandbox.effective_languages,
                expect_prebaked=prebaked_expected,
            )
        ]
        if github is not None:
            installs.append(partial(github.install, extras="", expect_prebaked=prebaked_expected))
        if len(installs) == 1:
            installs[0]()
        else:
            with ThreadPoolExecutor(
                max_workers=len(installs), thread_name_prefix="sbxloop-install"
            ) as pool:
                futures = [pool.submit(fn) for fn in installs]
                errors: list[Exception] = []
                for role, future in zip(("agent", "github"), futures, strict=True):
                    try:
                        future.result()
                    except Exception as exc:
                        # Only the first is raised; log each so the second
                        # sandbox's failure is not lost with it.
                        log.warning(
                            "worker.install_failed",
                            run=run_id,
                            role=role,
                            error=str(exc),
                            exc_info=len(errors) > 0,
                        )
                        errors.append(exc)
                if errors:
                    raise errors[0]
        if prebaked_expected:
            self._emit_prebaked(run_id, pair, agent, github)

    def _emit_prebaked(
        self,
        run_id: str,
        pair: SandboxPair,
        agent: WorkerClient,
        github: WorkerClient | None,
    ) -> None:
        """One event per sandbox saying whether the configured template's
        baked worker was used, or was stale and the install ladder ran."""
        clients = [(pair.agent.name, agent)]
        if github is not None and pair.github is not None:
            clients.append((pair.github.name, github))
        for name, client in clients:
            message = (
                "prebaked worker verified; install skipped"
                if client.prebaked
                else "template not prebaked or stale; ran the install ladder "
                "(re-run `sbxloop bake` to refresh)"
            )
            self.bus.emit(
                HostEventTypes.SANDBOX_PREBAKED,
                run_id,
                name=name,
                template=self.config.sandbox.template,
                prebaked=client.prebaked,
                message=message,
            )

    @staticmethod
    def _pair_names(pair: SandboxPair) -> list[str]:
        return [s.name for s in (pair.agent, pair.github) if s is not None]

    def _keep_on_failure(self, run_id: str, pair: SandboxPair) -> None:
        """Flip the pair to kept when configured, so a failed run's evidence
        survives for `sbxloop shell`. Marked in the DB for `sandbox prune`."""
        if not self.config.keep_on_failure or pair.keep:
            return
        pair.keep = True
        self.store.set_run_kept(run_id, "debug")
        names = self._pair_names(pair)
        self.bus.emit(
            HostEventTypes.RUN_KEEP,
            run_id,
            sandboxes=names,
            reason="debug",
            message=(
                f"sandboxes kept for debugging: {', '.join(names)} — "
                f"inspect with `sbxloop shell {run_id}`"
            ),
        )

    def _attach_reporter(
        self, github: WorkerClient | None, run_id: str, outcome: str
    ) -> tuple[GithubReporterHook | None, Callable[[], None]]:
        """Attach progress reporting; opens the tracking issue immediately.

        Run start/end go through explicit ``open_run``/``close_run`` calls
        rather than bus events: RUN_START is emitted before the github
        sandbox exists and RUN_END after it is gone, so the hook could never
        observe them (#58).
        """
        gh = self.config.github
        if not gh.report or github is None:
            return None, lambda: None
        assert gh.repo is not None  # report=True without a repo cannot provision a github worker
        hook = GithubReporterHook(GithubOps(github, run_id), gh.repo)
        detach = self.bus.attach_hook(hook)
        hook.open_run(run_id, outcome)
        if hook.issue is not None:
            # Persisted so the finish summary (and any later reader of the
            # event stream) can point at the tracking issue.
            self.bus.emit(
                HostEventTypes.RUN_REPORT,
                run_id,
                repo=gh.repo,
                issue=hook.issue.number,
                url=hook.issue.url,
            )
        return hook, detach

    def _harvest(self, run_id: str, pair: SandboxPair) -> None:
        """Copy the in-VM work dir out to the host (unmounted runs only).

        Best-effort by design: a failed copy must never fail the run.  Uses
        ``tar`` inside the VM with the configured ``artifacts.exclude`` entries
        so that ``.git``, venvs, and other heavy dirs are never transferred —
        the excluded content is not delivered anyway.  The tarball is staged in
        the VM's ``.sbxloop`` dir, copied out, and extracted on the host.
        """
        if pair.mounted:
            return
        target = self.config.state_dir / "runs" / run_id / "artifacts"
        target.mkdir(parents=True, exist_ok=True)
        exclude = self.config.artifacts.exclude
        # Build tar exclude flags: --exclude=<name> for each entry.
        exclude_args = [arg for name in exclude for arg in ("--exclude", name)]
        vm_tar = f"{SBXLOOP_DIR}/harvest.tar"
        started = time.monotonic()
        try:
            result = pair.agent.exec(
                ["tar", "-cf", vm_tar, "-C", pair.agent_workdir, *exclude_args, "."]
            )
            if not result.ok:
                raise SbxError(
                    f"tar failed (exit {result.returncode})",
                    argv=result.argv,
                    stderr=result.stderr,
                )
            with tempfile.TemporaryDirectory() as tmpdir:
                host_tar = Path(tmpdir) / "harvest.tar"
                pair.agent.cp_out(vm_tar, host_tar)
                with tarfile.open(host_tar) as tf:
                    tf.extractall(target, filter="data")
        except SbxError:
            log.warning(
                "run.harvest_failed",
                run=run_id,
                target=str(target),
                duration_s=round(time.monotonic() - started, 1),
                exc_info=True,
            )
            return
        log.info(
            "run.harvested",
            run=run_id,
            target=str(target),
            duration_s=round(time.monotonic() - started, 1),
        )

    def _report_artifacts(self, run_id: str, pair: SandboxPair) -> None:
        target = (
            pair.workspace
            if pair.mounted
            else self.config.state_dir / "runs" / run_id / "artifacts"
        )
        if target is None or not target.is_dir():
            return
        scan = scan_artifacts(target, self.config.artifacts.exclude)
        extra: dict[str, Any] = {}
        if scan.excluded:
            # Surface what the listing/delivery resolvers leave out — silent
            # truncation is the bug (#67).
            extra["excluded"] = dict(scan.excluded)
        sample = self._last_resources.get("agent")
        if sample and sample.get("level") in ("warn", "abort"):
            # Disk was under pressure at the last sample — harvested
            # artifacts may be truncated or missing.
            extra["disk_used_pct"] = sample.get("disk_used_pct")
            extra["resources_level"] = sample.get("level")
            log.warning(
                "run.artifacts_maybe_incomplete",
                run=run_id,
                disk_used_pct=sample.get("disk_used_pct"),
                resources_level=sample.get("level"),
                hint="sandbox disk was under pressure at the last sample",
            )
        self.bus.emit(
            HostEventTypes.RUN_ARTIFACTS,
            run_id,
            path=str(target),
            files=len(scan.files),
            mounted=pair.mounted,
            **extra,
        )

    def _ensure_delivery_repo(self, run_id: str, github: WorkerClient | None) -> None:
        """Probe (and, when allowed, create) the delivery repo up front.

        Runs right after worker install so a missing or typo'd repository
        fails the run before any planning or execution happens, not after
        the work is done. A creation is surfaced as a run.deliver event so
        the transcript records where the artifacts will land.
        """
        gh = self.config.github
        if not gh.deliver or not gh.repo or github is None:
            return
        created = ensure_repository(
            GithubOps(github, run_id),
            gh.repo,
            create=gh.create_repo,
            public=gh.create_public,
        )
        if created:
            self.bus.emit(HostEventTypes.RUN_DELIVER, run_id, repo=gh.repo, created=True)

    def _deliver(
        self, run_id: str, outcome: str, pair: SandboxPair, github: WorkerClient | None
    ) -> None:
        """Publish the completed run's artifacts as a PR to the configured
        [github].repo when delivery is enabled. The run has already
        succeeded — delivery failure is loud (run.deliver event with the
        error) but never changes the run state.
        """
        gh = self.config.github
        repo = gh.repo
        if not gh.deliver or not repo or github is None:
            return
        source = (
            pair.workspace
            if pair.mounted
            else self.config.state_dir / "runs" / run_id / "artifacts"
        )
        if source is None or not source.is_dir():
            self.bus.emit(
                HostEventTypes.RUN_DELIVER, run_id, repo=repo, error="no artifacts directory"
            )
            return
        started = time.monotonic()
        log.info(
            "run.deliver_start",
            run=run_id,
            repo=repo,
            base=gh.deliver_base,
            draft=gh.deliver_draft,
            source=str(source),
        )
        try:
            pr = deliver_workspace(
                GithubOps(github, run_id),
                repo,
                run_id=run_id,
                outcome=outcome,
                source_dir=source,
                base=gh.deliver_base,
                draft=gh.deliver_draft,
                exclude=self.config.artifacts.exclude,
            )
        except SbxloopError as exc:
            # Catches the whole family the delivery path can raise — not just
            # DeliveryError/GithubOpsError but WorkerError/WorkerTimeoutError/
            # SbxError from the op jobs themselves. Anything narrower lets an
            # infra hiccup during this optional post-completion step escape
            # _drive and leave the completed run looking failed (#59).
            log.warning(
                "run.deliver_failed",
                run=run_id,
                repo=repo,
                duration_s=round(time.monotonic() - started, 1),
                exc_info=True,
            )
            self.bus.emit(HostEventTypes.RUN_DELIVER, run_id, repo=repo, error=str(exc))
            return
        log.info(
            "run.delivered",
            run=run_id,
            repo=repo,
            pr=pr.number,
            url=pr.url,
            duration_s=round(time.monotonic() - started, 1),
        )
        self.bus.emit(HostEventTypes.RUN_DELIVER, run_id, repo=repo, pr=pr.number, url=pr.url)

    def _run_phases(
        self,
        run_id: str,
        phases: PhaseRunner,
        deadline: float,
        pair: SandboxPair,
        granter: EgressGranter,
    ) -> RunState:
        tasks = self.store.get_tasks(run_id)
        if not tasks:
            self._set_run_state(run_id, "decomposing")
            started = time.time()
            graph = phases.decompose()
            if len(graph.tasks) > self.config.budgets.max_tasks:
                raise BudgetExceededError(
                    f"decomposition produced {len(graph.tasks)} tasks "
                    f"(max {self.config.budgets.max_tasks})"
                )
            ordered = graph.topo_order()
            self.store.save_tasks(run_id, ordered)
            self.store.record_phase(
                run_id,
                "decompose",
                task_id=None,
                attempt=1,
                status="ok",
                output_json=graph.model_dump_json(),
                started_at=started,
            )
            tasks = self.store.get_tasks(run_id)

        self._set_run_state(run_id, "running")
        # Announce the full roster up front (with titles) so UIs can show
        # every task waiting immediately, instead of revealing rows one at a
        # time as each prior task finishes. Also runs on resume, where it
        # restores the table with each task's persisted state.
        for task in tasks:
            self.bus.emit(
                HostEventTypes.TASK_STATE,
                run_id,
                task_id=task.spec.id,
                title=task.spec.title,
                state=task.state,
                revisions=task.revisions,
                replans=task.replans,
            )
        # The same roster as one event, for a surface that cannot hold a live
        # table (Discord edits one status line, which has room for the current
        # task only). Without it the decomposition reaches a human nowhere:
        # the decomposer's own reply is JSON, and JSON is not posted.
        self.bus.emit(
            HostEventTypes.RUN_TASKS,
            run_id,
            message=f"{len(tasks)} task(s)",
            tasks=[
                {
                    "id": task.spec.id,
                    "title": task.spec.title,
                    "state": task.state,
                    "depends_on": list(task.spec.depends_on),
                }
                for task in tasks
            ],
        )
        failed_ids, skipped_ids = self._schedule_tasks(
            run_id, phases, tasks, deadline, pair, granter
        )

        # Final drain: messages that arrived during the last phase still get
        # answered (as steer_run — there is no task left to steer).
        self._process_chat(run_id, phases, None)
        self._set_run_state(run_id, "finalizing")
        return "failed" if failed_ids or skipped_ids else "completed"

    def _schedule_tasks(
        self,
        run_id: str,
        phases: PhaseRunner,
        tasks: Sequence[TaskRecord],
        deadline: float,
        pair: SandboxPair,
        granter: EgressGranter,
    ) -> tuple[set[str], set[str]]:
        """Drive every non-terminal task, up to ``max_parallel_tasks`` at once.

        ``tasks`` arrives in dependency order, so at ``max_parallel_tasks=1``
        this walks it front to back and is exactly the serial loop it
        replaces: the first non-terminal task always has its dependencies
        behind it. Above 1, readiness is evaluated explicitly instead of
        being implied by position — a task may start once every dependency
        has *finished*, and is skipped if any of them failed or was skipped.

        The first infrastructure error stops new launches but does not
        abandon the lanes already running: they are allowed to finish so
        their state is checkpointed (which is what makes the run resumable),
        and the error is re-raised afterwards.
        """
        done_ids = {t.spec.id for t in tasks if t.state == "done"}
        failed_ids = {t.spec.id for t in tasks if t.state == "failed"}
        skipped_ids = {t.spec.id for t in tasks if t.state == "skipped"}
        waiting = [t for t in tasks if not t.terminal]
        lanes = max(1, self.config.budgets.max_parallel_tasks)

        def finished(task: TaskRecord) -> None:
            if task.state == "failed":
                failed_ids.add(task.spec.id)
            elif task.state == "skipped":
                skipped_ids.add(task.spec.id)
            else:
                done_ids.add(task.spec.id)

        def ready(task: TaskRecord) -> bool:
            return all(dep in done_ids | failed_ids | skipped_ids for dep in task.spec.depends_on)

        running: dict[Future[None], TaskRecord] = {}
        failure: BaseException | None = None
        pool = ThreadPoolExecutor(max_workers=lanes, thread_name_prefix=f"sbxloop-task-{run_id}")
        try:
            while waiting or running:
                while waiting and len(running) < lanes and failure is None:
                    task = next((t for t in waiting if ready(t)), None)
                    if task is None:
                        break
                    waiting.remove(task)
                    blocked = [d for d in task.spec.depends_on if d in failed_ids | skipped_ids]
                    if blocked:
                        task.state = "skipped"
                        skipped_ids.add(task.spec.id)
                        self.store.update_task(run_id, task)
                        self._emit_task_end(run_id, task)
                        continue
                    running[
                        pool.submit(self._run_task, run_id, phases, task, deadline, pair, granter)
                    ] = task
                if not running:
                    # Nothing in flight and nothing launchable. Either every
                    # task is accounted for, or a lane failed and stopped new
                    # launches — the error is re-raised below. An acyclic
                    # graph with no failure always leaves something ready, so
                    # this is the normal exit, not a stall.
                    break
                for future in wait(running, return_when=FIRST_COMPLETED).done:
                    task = running.pop(future)
                    try:
                        future.result()
                    except BaseException as exc:
                        failure = failure or exc
                    finished(task)
        finally:
            pool.shutdown(wait=True)
        if failure is not None:
            raise failure
        return failed_ids, skipped_ids

    # -- task state machine ------------------------------------------------

    def _run_task(
        self,
        run_id: str,
        phases: PhaseRunner,
        task: TaskRecord,
        deadline: float,
        pair: SandboxPair,
        granter: EgressGranter,
    ) -> None:
        budgets = self.config.budgets
        if task.state == "pending":
            self._set_task_state(run_id, task, "planning")
        self.bus.emit(
            HostEventTypes.TASK_START, run_id, task_id=task.spec.id, title=task.spec.title
        )
        # Resume mapping: executing/scrutinizing restart at executing (the
        # plan is persisted); a missing plan always restarts at planning.
        if task.state in ("executing", "scrutinizing") and task.plan is None:
            self._set_task_state(run_id, task, "planning")
        if task.state in ("verifying", "validating") and task.plan is None:
            self._set_task_state(run_id, task, "planning")
        if task.state == "scrutinizing":
            self._set_task_state(run_id, task, "executing")

        while not task.terminal:
            self._check_cancelled_and_clock(run_id, deadline)
            # Interactive chat: absorb queued user messages at the same
            # boundary cancellation uses — the agent pauses here, replies,
            # and any course change (re-plan, standing guidance) lands
            # before the next phase runs.
            self._process_chat(run_id, phases, self._steer_target(task))
            abort_reason = self._resource_abort_reason()
            if abort_reason:
                # Fail the task with a diagnosis instead of letting the next
                # phase produce garbage in a full sandbox. Dependents are
                # skipped by the normal failed-task machinery.
                task.last_feedback = abort_reason
                self._set_task_state(run_id, task, "failed")
                break
            if task.state == "planning":
                self._phase_plan(run_id, phases, task)
            elif task.state == "executing":
                self._phase_execute_and_scrutinize(run_id, phases, task, granter)
            elif task.state == "verifying":
                self._phase_verify(run_id, phases, task, budgets)
            elif task.state == "validating":
                self._phase_validate(run_id, phases, task, budgets)
            else:  # pragma: no cover - defensive
                raise StateError(f"task {task.spec.id} in unexpected state {task.state}")

        # Task-boundary harvest narrows the loss window on long runs; the
        # finalize harvest in _drive remains the authoritative sweep.
        # When harvest_mode is "final", skip the mid-run copy for cheaper
        # per-task cost on runs with large workspaces.
        if self.config.artifacts.harvest_mode == "per-task":
            # Copies the whole workspace out of the shared sandbox; two lanes
            # harvesting at once would interleave into the same directory.
            with self._sandbox_lock:
                self._harvest(run_id, pair)
        self._emit_task_end(run_id, task)

    def _phase_plan(self, run_id: str, phases: PhaseRunner, task: TaskRecord) -> None:
        started = time.time()
        plan = phases.plan(task)
        task.plan = plan
        # What the task will actually be executed against, as parsed data:
        # the planner answers in JSON, so this is the only form of the plan a
        # human ever sees.
        self.bus.emit(
            HostEventTypes.PHASE_PLAN,
            run_id,
            task_id=task.spec.id,
            attempt=task.replans + 1,
            message=f"{len(plan.steps)} step(s)",
            steps=list(plan.steps),
            expected_artifacts=list(plan.expected_artifacts),
            verify_commands=list(plan.verify_commands),
            egress=[{"domain": e.domain, "reason": e.reason} for e in plan.egress],
        )
        self.store.record_phase(
            run_id,
            "plan",
            task_id=task.spec.id,
            attempt=task.replans + 1,
            status="ok",
            output_json=plan.model_dump_json(),
            started_at=started,
        )
        self._set_task_state(run_id, task, "executing")

    def _phase_execute_and_scrutinize(
        self,
        run_id: str,
        phases: PhaseRunner,
        task: TaskRecord,
        granter: EgressGranter,
    ) -> None:
        assert task.plan is not None
        # Grant-late: plan-declared egress is applied at EXECUTE entry, not
        # at plan time. Runs here (not in _phase_plan) so resumed tasks whose
        # persisted state skips planning still get their grants on the
        # freshly provisioned sandbox.
        # The grant rewrites the shared sandbox's network policy, so lanes
        # take it in turn rather than interleaving inside it.
        with self._sandbox_lock:
            granter.apply(
                task.spec.id, [(egress.domain, egress.reason) for egress in task.plan.egress]
            )
        started = time.time()
        result = phases.execute(task, task.plan)
        task.session_id = result.session_id
        executor_report = clip(result.output_text)
        self.store.record_phase(
            run_id,
            "execute",
            task_id=task.spec.id,
            attempt=task.revisions + 1,
            status="ok",
            output_json=json.dumps({"report": executor_report, "session_id": result.session_id}),
            started_at=started,
        )
        self._set_task_state(run_id, task, "scrutinizing")

        started = time.time()
        outcome = phases.scrutinize(task, task.plan, executor_report)
        verdict = outcome.verdict
        self.store.record_phase(
            run_id,
            "scrutinize",
            task_id=task.spec.id,
            attempt=task.revisions + 1,
            status=verdict.verdict,
            output_json=json.dumps(self._critic_payload(outcome)),
            started_at=started,
        )
        self._emit_verdict(run_id, task, "scrutinize", verdict)
        self._emit_critic_degraded(run_id, task, "scrutinize", outcome)
        if self._replan_suspect_verify(run_id, task, verdict):
            return
        if verdict.verdict == "revise":
            self._register_revision(run_id, task, verdict.feedback or "scrutiny found issues")
            return
        task.last_feedback = ""
        self._set_task_state(run_id, task, "verifying")

    def _last_verify_failure(self, run_id: str, task: TaskRecord) -> str | None:
        """The verify feedback that triggered the revision now under review,
        or None if that revision was not verify-triggered.

        Read from the persisted verify attempt rather than
        ``task.last_feedback``: feedback text is agent-authored (a critic's
        ``revise`` may open with anything, including the verify-failure
        wording), so only the phase ledger can say what actually ran. The
        attempt numbers line up because verify runs at attempt ``revisions
        + 1`` and a failure bumps ``revisions`` before the next execute — so
        the last verify is the trigger iff its attempt equals the current
        revision count. A replan resets revisions to 0, which no verify
        attempt can match, so the old plan's failures do not carry over.
        """
        row = self.store.latest_phase_attempt(run_id, task.spec.id, "verify")
        if row is None or row["status"] != "failed" or row["attempt"] != task.revisions:
            return None
        try:
            feedback = json.loads(row["output_json"] or "{}").get("feedback")
        except ValueError:
            return None
        return feedback if isinstance(feedback, str) else None

    def _replan_suspect_verify(self, run_id: str, task: TaskRecord, verdict: Verdict) -> bool:
        """Spend a replan now when the scrutinizer passed the work but ruled
        the verify command itself wrong (#231). Returns True if the task was
        sent back to planning.

        Field failure r567rsm4e: a portable, runnable check asserting an
        ``od`` column layout that never matches — correct code, wrong check,
        130+ executor tool calls across revisions before verify exhaustion
        finally replanned (#94). The scrutinizer is the one stage that sees
        the failing command next to the passing code, so its ruling is the
        earliest point the loop can act.

        The ruling is only honored on a ``pass`` — a ``revise`` means the
        work is not done either, and the executor's fix comes first — and
        only when backed by evidence: the revision being reviewed was itself
        triggered by a verify failure (read from the persisted verify
        attempt, not from feedback text), so a speculative flag on a check
        that has never run cannot cost a replan on a fine plan; and only
        within the replan budget, since a task that has no replans left can
        only fail bounded, not loop. Either way the ruling is put in the
        live stream: silently ignoring a critic's finding is the
        transcript-only failure #94 already fixed once.
        """
        if not verdict.verify_suspect:
            return False
        reason = verdict.verify_suspect_reason
        failure = self._last_verify_failure(run_id, task)
        budget = task.replans < self.config.budgets.max_replans_per_task
        honored = verdict.verdict == "pass" and failure is not None and budget
        if honored:
            message = f"scrutinizer passed the work but ruled the verify command wrong: {reason}"
        elif verdict.verdict != "pass":
            message = (
                "scrutinizer flagged the verify command as wrong but asked for "
                f"revisions first; the work is revised before the check is judged: {reason}"
            )
        elif failure is None:
            message = (
                "scrutinizer flagged the verify command as wrong before it has "
                f"failed; ignored until verify runs: {reason}"
            )
        else:
            message = (
                "scrutinizer flagged the verify command as wrong but no replan "
                f"budget remains; verifying anyway: {reason}"
            )
        self.bus.emit(
            HostEventTypes.PHASE_END,
            run_id,
            task_id=task.spec.id,
            phase="scrutinize",
            status="verify_suspect",
            honored=honored,
            message=message,
        )
        if not honored:
            return False
        task.replans += 1
        task.plan = None
        task.revisions = 0
        task.last_feedback = (
            "the reviewer judged the work correct and the verify command itself "
            f"wrong: {reason}\n\nWrite a plan whose verify_commands check what "
            "the task actually requires — prefer the project's test runner over "
            "shell pipelines. The failing check was:\n\n" + (failure or "")
        )
        self._set_task_state(run_id, task, "planning")
        return True

    def _phase_verify(
        self,
        run_id: str,
        phases: PhaseRunner,
        task: TaskRecord,
        budgets: object,
    ) -> None:
        assert task.plan is not None
        started = time.time()
        passed, feedback, results = phases.verify(task, task.plan)
        # `results` (the full command transcript) is persisted so VALIDATE —
        # including one entered on resume in a fresh process — reads its
        # evidence from phase_attempts rather than in-memory state (#61).
        self.store.record_phase(
            run_id,
            "verify",
            task_id=task.spec.id,
            attempt=task.revisions + 1,
            status="ok" if passed else "failed",
            output_json=json.dumps(
                {"passed": passed, "feedback": clip(feedback), "results": results}
            ),
            started_at=started,
        )
        if passed:
            self._set_task_state(run_id, task, "validating")
            return
        # Put the failing command in the live stream: without this the
        # transcript jumps verifying -> failed and the reason only exists
        # in the phase_attempts table.
        failure_count = feedback.count(VERIFY_FAILURE_PREFIX)
        first_line = feedback.splitlines()[0] if feedback else "verify failed"
        self.bus.emit(
            HostEventTypes.PHASE_END,
            run_id,
            task_id=task.spec.id,
            phase="verify",
            status="failed",
            message=(
                first_line if failure_count <= 1 else f"{first_line} (+{failure_count - 1} more)"
            ),
        )
        self._register_revision(run_id, task, feedback, verify_failure=True)

    def _phase_validate(
        self,
        run_id: str,
        phases: PhaseRunner,
        task: TaskRecord,
        budgets: object,
    ) -> None:
        verify_results = self._verify_evidence(run_id, task)
        if verify_results is None:
            # No committed verify transcript for this task (a pre-upgrade
            # checkpoint resumed into `validating`): VERIFY is mechanical and
            # idempotent, so rewind and repopulate the evidence rather than
            # asking the judge to rule without it.
            self._set_task_state(run_id, task, "verifying")
            return
        started = time.time()
        outcome = phases.validate(task, verify_results)
        verdict = outcome.verdict
        self.store.record_phase(
            run_id,
            "validate",
            task_id=task.spec.id,
            attempt=task.replans + 1,
            status=verdict.verdict,
            output_json=json.dumps(self._critic_payload(outcome)),
            started_at=started,
        )
        self._emit_verdict(run_id, task, "validate", verdict)
        self._emit_critic_degraded(run_id, task, "validate", outcome)
        if verdict.verdict == "accept":
            self._set_task_state(run_id, task, "done")
            return
        task.replans += 1
        if task.replans > self.config.budgets.max_replans_per_task:
            task.last_feedback = verdict.feedback
            self._set_task_state(run_id, task, "failed")
            return
        task.last_feedback = verdict.feedback or "validation rejected the result"
        task.plan = None
        task.revisions = 0
        self._set_task_state(run_id, task, "planning")

    @staticmethod
    def _critic_payload(outcome: CriticOutcome) -> dict[str, Any]:
        """The phase-row payload for a critic verdict: the verdict itself
        plus the session's tooling health and whether the guard downgraded a
        clean verdict — degraded critic runs must be auditable after the
        fact (#123)."""
        payload: dict[str, Any] = outcome.verdict.model_dump()
        if outcome.health is not None:
            payload["tooling_health"] = outcome.health.model_dump()
        if outcome.downgraded:
            payload["downgraded"] = True
        return payload

    def _emit_verdict(self, run_id: str, task: TaskRecord, phase: str, verdict: Verdict) -> None:
        """Put a critic's ruling — and what it rested on — in the live stream.

        The critics answer in JSON, so without this the reasoning behind a
        revise/reject reaches a human nowhere: the task state says
        ``executing`` again and the feedback only exists in the
        ``phase_attempts`` ledger.
        """
        self.bus.emit(
            HostEventTypes.PHASE_VERDICT,
            run_id,
            task_id=task.spec.id,
            phase=phase,
            verdict=verdict.verdict,
            attempt=task.revisions + 1,
            message=f"{phase}: {verdict.verdict}",
            issues=[{"severity": i.severity, "detail": i.detail} for i in verdict.issues],
            feedback=verdict.feedback,
        )

    def _emit_critic_degraded(
        self, run_id: str, task: TaskRecord, phase: str, outcome: CriticOutcome
    ) -> None:
        """Put a downgrade in the live stream: without this the transcript
        shows an ordinary revise/reject and the real reason (a critic that
        lost its tooling) only exists in the phase_attempts table."""
        if not outcome.downgraded:
            return
        assert outcome.health is not None
        self.bus.emit(
            HostEventTypes.PHASE_END,
            run_id,
            task_id=task.spec.id,
            phase=phase,
            status="degraded",
            message=(
                f"critic tooling degraded ({outcome.health.summary()}); "
                f"its clean verdict was not trusted and was downgraded to "
                f"{outcome.verdict.verdict!r}"
            ),
        )

    def _verify_evidence(self, run_id: str, task: TaskRecord) -> str | None:
        """The task's latest committed verify-command transcript, or None.

        phase_attempts is the single source of truth for this evidence: a
        resumed run's VALIDATE judges with exactly what a fresh one would.
        """
        output = self.store.latest_phase_output(run_id, task.spec.id, "verify")
        if output is None:
            return None
        results = json.loads(output).get("results")
        return results if isinstance(results, str) else None

    def _register_revision(
        self, run_id: str, task: TaskRecord, feedback: str, *, verify_failure: bool = False
    ) -> None:
        task.revisions += 1
        task.last_feedback = feedback
        if task.revisions <= self.config.budgets.max_revisions_per_task:
            self._set_task_state(run_id, task, "executing")
            return
        if verify_failure and task.replans < self.config.budgets.max_replans_per_task:
            # Verify commands come from the plan and task spec; the executor
            # cannot edit them, so no number of revisions can fix a check
            # that disagrees with where the work landed. A fresh plan
            # regenerates its verify_commands and can route steps to where
            # the spec-level commands expect files.
            task.replans += 1
            task.plan = None
            task.revisions = 0
            task.last_feedback = (
                "every revision failed the same verify commands; write a plan "
                "whose steps and verify_commands agree on file locations and "
                "setup:\n\n" + feedback
            )
            self._set_task_state(run_id, task, "planning")
            return
        self._set_task_state(run_id, task, "failed")

    # -- interactive chat --------------------------------------------------

    def _process_chat(self, run_id: str, phases: PhaseRunner, task: TaskRecord | None) -> None:
        """Drain queued user messages: one STEER session each, FIFO.

        A failed steer never fails the run — the error rides on the
        ``chat.reply`` event and the message is dropped; real infrastructure
        breakage will surface loudly in the next phase anyway.

        One lane at a time: every task lane reaches a phase boundary and
        calls this, but the mailbox only needs draining once. The lock is
        taken non-blocking so the lanes that lose the race carry straight on
        into their next phase instead of queueing behind a steer session's
        LLM round trip — the messages are still answered, by the lane that
        holds it, and standing guidance lands before any lane's next prompt.
        """
        if not self._chat_lock.acquire(blocking=False):
            return
        try:
            self._drain_chat(run_id, phases, task)
        finally:
            self._chat_lock.release()

    def _drain_chat(self, run_id: str, phases: PhaseRunner, task: TaskRecord | None) -> None:
        while True:
            try:
                message = self._chat_queue.get_nowait()
            except queue.Empty:
                return
            self.bus.emit(
                HostEventTypes.CHAT_MESSAGE,
                run_id,
                message_id=message.message_id,
                text=message.text,
            )
            self._steer_attempts += 1
            started = time.time()
            try:
                verdict = phases.steer(message.text, tasks=self.store.get_tasks(run_id), task=task)
            except WorkerError as exc:
                log.warning(
                    "run.steer_failed",
                    run=run_id,
                    message=message.message_id,
                    attempt=self._steer_attempts,
                    exc_info=True,
                )
                self.store.record_phase(
                    run_id,
                    "steer",
                    task_id=task.spec.id if task else None,
                    attempt=self._steer_attempts,
                    status="error",
                    output_json=json.dumps({"message": message.text, "error": str(exc)}),
                    started_at=started,
                )
                self.bus.emit(
                    HostEventTypes.CHAT_REPLY,
                    run_id,
                    message_id=message.message_id,
                    error=str(exc),
                )
                continue
            action = self._apply_steer(run_id, task, verdict, phases)
            self.store.record_phase(
                run_id,
                "steer",
                task_id=task.spec.id if task else None,
                attempt=self._steer_attempts,
                status=action,
                output_json=json.dumps(
                    {"message": message.text} | verdict.model_dump() | {"applied": action}
                ),
                started_at=started,
            )
            self.bus.emit(
                HostEventTypes.CHAT_REPLY,
                run_id,
                message_id=message.message_id,
                reply=verdict.reply,
                action=action,
            )

    def _steer_target(self, task: TaskRecord) -> TaskRecord | None:
        """The task a steer verdict may re-plan, or None for run-level only.

        Only a lone lane offers itself. With several in flight there is no
        "current task": the lane that wins the chat lock is whichever
        reached a phase boundary first, so steering it would re-plan an
        arbitrary task rather than the one the operator meant. None routes
        the verdict through the existing ``steer_task`` -> ``steer_run``
        downgrade in :meth:`_apply_steer`, recording the guidance for every
        later prompt instead of gambling on a lane. Steering one task by
        name under parallelism needs an explicit target plus a barrier
        holding that lane at its boundary until the verdict lands — a
        feature to design, not a default to fall into.
        """
        return task if self.config.budgets.max_parallel_tasks == 1 else None

    def _apply_steer(
        self,
        run_id: str,
        task: TaskRecord | None,
        verdict: SteerVerdict,
        phases: PhaseRunner,
    ) -> str:
        """Apply a steer verdict's course change; returns the action actually
        applied (``steer_task`` downgrades to ``steer_run`` when no task is
        live to steer)."""
        action = verdict.action
        if action == "steer_task" and (task is None or task.terminal):
            action = "steer_run"
        if action == "steer_task":
            assert task is not None
            # User direction, not a failure: the task re-plans with the
            # guidance as feedback, and neither budget counter is spent.
            task.last_feedback = f"user steering (must be honored): {verdict.guidance}"
            task.plan = None
            task.revisions = 0
            self._set_task_state(run_id, task, "planning")
            self.bus.emit(
                HostEventTypes.CHAT_ACTION,
                run_id,
                task_id=task.spec.id,
                action=action,
                guidance=verdict.guidance,
                message=f"user steering: re-planning task {task.spec.id} — {verdict.guidance}",
            )
        elif action == "steer_run":
            self.store.append_run_guidance(run_id, verdict.guidance)
            phases.add_guidance(verdict.guidance)
            self.bus.emit(
                HostEventTypes.CHAT_ACTION,
                run_id,
                action=action,
                guidance=verdict.guidance,
                message=f"user steering: standing guidance added — {verdict.guidance}",
            )
        return action

    # -- bookkeeping -------------------------------------------------------

    def _resource_abort_reason(self) -> str | None:
        """Non-None when the agent sandbox's last resource sample crossed
        an abort threshold (the worker classifies; the level rides on the
        event). Names the resource that tripped so an OOM-bound task is not
        diagnosed as a full disk (#253)."""
        sample = self._last_resources.get("agent")
        if not sample or sample.get("level") != "abort":
            return None
        limits = self.config.limits
        disk = sample.get("disk_used_pct")
        if isinstance(disk, (int, float)) and limits.disk_abort > 0 and disk >= limits.disk_abort:
            return (
                f"sandbox disk exhausted: {disk}% of the workspace filesystem is used "
                f"(limits.disk_abort={limits.disk_abort}%)"
            )
        return (
            f"sandbox memory exhausted: {sample.get('mem_used_pct')}% of memory is used "
            f"(limits.mem_abort={limits.mem_abort}%)"
        )

    def _check_cancelled_and_clock(self, run_id: str, deadline: float) -> None:
        if self._cancel_event.is_set():
            raise RunCancelledError(
                f"run {run_id} interrupted; resume with `sbxloop resume {run_id}`"
            )
        if self.store.get_run(run_id).state == "cancelled":
            raise RunCancelledError(f"run {run_id} was cancelled")
        if self.clock() > deadline:
            self._set_run_state(run_id, "failed")
            raise BudgetExceededError(
                f"run {run_id} exceeded max_wall_clock_s={self.config.budgets.max_wall_clock_s}"
            )

    def _set_run_state(self, run_id: str, state: RunState) -> None:
        self.store.set_run_state(run_id, state)
        self.bus.emit(HostEventTypes.RUN_STATE, run_id, state=state)

    def _set_task_state(self, run_id: str, task: TaskRecord, state: TaskState) -> None:
        task.state = state
        self.store.update_task(run_id, task)
        self.bus.emit(
            HostEventTypes.TASK_STATE,
            run_id,
            task_id=task.spec.id,
            state=state,
            revisions=task.revisions,
            replans=task.replans,
        )

    def _emit_task_end(self, run_id: str, task: TaskRecord) -> None:
        self.bus.emit(
            HostEventTypes.TASK_END,
            run_id,
            task_id=task.spec.id,
            title=task.spec.title,
            state=task.state,
        )


def run_outcome(outcome: str, config: Config | None = None) -> RunResult:
    """Convenience one-shot API: run an outcome with default wiring."""
    return LoopEngine(config or load_config()).start(outcome)
