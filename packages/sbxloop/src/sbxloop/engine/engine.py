"""LoopEngine: drives DECOMPOSE → (PLAN → EXECUTE → SCRUTINIZE → VERIFY →
VALIDATE)* under budgets, with SQLite checkpointing after every transition.

Failure semantics:

- Budget exhaustion (revisions/replans) fails the *task*; dependents are
  skipped and the run continues, finishing ``failed`` if any task failed.
  One exception: revisions exhausted by *verify-command* failures spend a
  replan first when budget remains — the executor cannot edit verify
  commands, so only a fresh plan can unstick a broken check.
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
import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from sbxloop.config import Config, _flatten, load_config, load_dotenv_file
from sbxloop.deliver import deliver_workspace
from sbxloop.engine.model import (
    RESUMABLE_RUN_STATES,
    RunResult,
    RunState,
    TaskRecord,
    TaskState,
)
from sbxloop.engine.phases import PhaseRunner, clip
from sbxloop.engine.store import StateStore
from sbxloop.errors import (
    BudgetExceededError,
    SbxError,
    SdxloopError,
    StateError,
)
from sbxloop.events import EventBus, Hook, HostEventTypes
from sbxloop.gh.ops import GithubOps
from sbxloop.gh.reporter import GithubReporterHook
from sbxloop.ids import new_run_id
from sbxloop.policy import EgressGranter
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.pair import SandboxPair
from sbxloop.sbx.provision import Provisioner
from sbxloop.worker.client import WorkerClient

logger = logging.getLogger(__name__)


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
        # Latest sandbox.resources sample per sandbox role, fed by the bus;
        # consulted for the disk guardrail and the harvest-truncation note.
        self._last_resources: dict[str, dict[str, object]] = {}
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
        self._rehydrate_config(run_id)
        self.bus.emit(HostEventTypes.RUN_START, run_id, outcome=run.outcome, resumed=True)
        return self._drive(run_id, run.outcome, workspace=run.workspace)

    def cancel(self, run_id: str) -> None:
        self.store.get_run(run_id)  # raises for unknown runs
        self.store.set_run_state(run_id, "cancelled")

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
            logger.warning("run %s: %s", run_id, message)
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
            logger.warning("run %s: %s", run_id, message)
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
                        # A configured template is expected to be prebaked
                        # (`sbxloop bake`): install() probes it and skips the
                        # ladder on success, falling back when stale.
                        prebaked_expected = bool(self.config.sandbox.template)
                        # ensure_dev_tools: the agent builds projects in this VM
                        # (venvs, pip) — the github sandbox only runs API ops.
                        agent.install(
                            extras="copilot",
                            ensure_dev_tools=True,
                            expect_prebaked=prebaked_expected,
                        )
                        if github is not None:
                            github.install(extras="", expect_prebaked=prebaked_expected)
                        if prebaked_expected:
                            self._emit_prebaked(run_id, pair, agent, github)
                    reporter, detach = self._attach_reporter(github, run_id, outcome)
                    try:
                        phases = PhaseRunner(
                            agent, self.config, run_id, outcome, workdir=pair.agent_workdir
                        )
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
                except SdxloopError:
                    # Infra failures (install, worker, sbx) are exactly what
                    # gets diagnosed in-sandbox; decide keep before pair exit.
                    self._keep_on_failure(run_id, pair)
                    raise
                if state == "completed":
                    self._deliver(run_id, outcome, pair, github)
                else:
                    self._keep_on_failure(run_id, pair)
        except SdxloopError:
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
        return hook, detach

    def _harvest(self, run_id: str, pair: SandboxPair) -> None:
        """Copy the in-VM work dir out to the host (unmounted runs only).

        Best-effort by design: a failed copy must never fail the run. The
        trailing ``/.`` copies directory *contents* (docker-style cp) — real
        sbx cp directory semantics are e2e-validated.
        """
        if pair.mounted:
            return
        target = self.config.state_dir / "runs" / run_id / "artifacts"
        target.mkdir(parents=True, exist_ok=True)
        try:
            pair.agent.cp_out(f"{pair.agent_workdir}/.", target)
        except SbxError:
            logger.warning("artifact harvest failed for run %s", run_id, exc_info=True)

    def _report_artifacts(self, run_id: str, pair: SandboxPair) -> None:
        target = (
            pair.workspace
            if pair.mounted
            else self.config.state_dir / "runs" / run_id / "artifacts"
        )
        if target is None or not target.is_dir():
            return
        count = sum(1 for p in target.rglob("*") if p.is_file())
        extra: dict[str, Any] = {}
        sample = self._last_resources.get("agent")
        if sample and sample.get("level") in ("warn", "abort"):
            # Disk was under pressure at the last sample — harvested
            # artifacts may be truncated or missing.
            extra = {
                "disk_used_pct": sample.get("disk_used_pct"),
                "resources_level": sample.get("level"),
            }
            logger.warning(
                "run %s: sandbox disk was at %s%% at the last sample — "
                "harvested artifacts may be incomplete",
                run_id,
                sample.get("disk_used_pct"),
            )
        self.bus.emit(
            HostEventTypes.RUN_ARTIFACTS,
            run_id,
            path=str(target),
            files=count,
            mounted=pair.mounted,
            **extra,
        )

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
        try:
            pr = deliver_workspace(
                GithubOps(github, run_id),
                repo,
                run_id=run_id,
                outcome=outcome,
                source_dir=source,
                base=gh.deliver_base,
                draft=gh.deliver_draft,
            )
        except SdxloopError as exc:
            # Catches the whole family the delivery path can raise — not just
            # DeliveryError/GithubOpsError but WorkerError/WorkerTimeoutError/
            # SbxError from the op jobs themselves. Anything narrower lets an
            # infra hiccup during this optional post-completion step escape
            # _drive and leave the completed run looking failed (#59).
            logger.warning("delivery to %s failed for run %s", repo, run_id, exc_info=True)
            self.bus.emit(HostEventTypes.RUN_DELIVER, run_id, repo=repo, error=str(exc))
            return
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
        failed_ids: set[str] = {t.spec.id for t in tasks if t.state == "failed"}
        skipped_ids: set[str] = {t.spec.id for t in tasks if t.state == "skipped"}
        for task in tasks:
            if task.terminal:
                failed_ids |= {task.spec.id} if task.state == "failed" else set()
                continue
            blocked = [d for d in task.spec.depends_on if d in failed_ids | skipped_ids]
            if blocked:
                task.state = "skipped"
                skipped_ids.add(task.spec.id)
                self.store.update_task(run_id, task)
                self._emit_task_end(run_id, task)
                continue
            self._run_task(run_id, phases, task, deadline, pair, granter)
            if task.state == "failed":
                failed_ids.add(task.spec.id)

        self._set_run_state(run_id, "finalizing")
        return "failed" if failed_ids or skipped_ids else "completed"

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
        self._harvest(run_id, pair)
        self._emit_task_end(run_id, task)

    def _phase_plan(self, run_id: str, phases: PhaseRunner, task: TaskRecord) -> None:
        started = time.time()
        plan = phases.plan(task)
        task.plan = plan
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
        granter.apply(task.spec.id, [(egress.domain, egress.reason) for egress in task.plan.egress])
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
        verdict = phases.scrutinize(task, task.plan, executor_report)
        self.store.record_phase(
            run_id,
            "scrutinize",
            task_id=task.spec.id,
            attempt=task.revisions + 1,
            status=verdict.verdict,
            output_json=verdict.model_dump_json(),
            started_at=started,
        )
        if verdict.verdict == "revise":
            self._register_revision(run_id, task, verdict.feedback or "scrutiny found issues")
        else:
            task.last_feedback = ""
            self._set_task_state(run_id, task, "verifying")

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
        failure_count = feedback.count("verify command failed:")
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
        verdict = phases.validate(task, verify_results)
        self.store.record_phase(
            run_id,
            "validate",
            task_id=task.spec.id,
            attempt=task.replans + 1,
            status=verdict.verdict,
            output_json=verdict.model_dump_json(),
            started_at=started,
        )
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

    # -- bookkeeping -------------------------------------------------------

    def _resource_abort_reason(self) -> str | None:
        """Non-None when the agent sandbox's last resource sample crossed
        the disk_abort threshold (the worker classifies; the level rides on
        the event)."""
        sample = self._last_resources.get("agent")
        if sample and sample.get("level") == "abort":
            return (
                f"sandbox disk exhausted: {sample.get('disk_used_pct')}% of the workspace "
                f"filesystem is used (limits.disk_abort={self.config.limits.disk_abort}%)"
            )
        return None

    def _check_cancelled_and_clock(self, run_id: str, deadline: float) -> None:
        if self.store.get_run(run_id).state == "cancelled":
            raise StateError(f"run {run_id} was cancelled")
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
