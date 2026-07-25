"""LoopEngine: drives DECOMPOSE → (PLAN → EXECUTE → SCRUTINIZE → VERIFY →
VALIDATE)* under budgets, with SQLite checkpointing after every transition.

Failure semantics:

- Budget exhaustion (revisions/replans) fails the *task*; dependents are
  skipped and the run continues, finishing ``failed`` if any task failed.
- Infrastructure errors (worker/sbx crashes) propagate after state is
  persisted — equivalent to a kill. ``resume()`` re-provisions a fresh
  sandbox pair (sandboxes are cattle; the workspace and SQLite state
  persist on the host) and continues from the last committed transition:
  a phase whose result was never committed re-runs from its start.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence

from sbxloop.config import Config, load_config, load_dotenv_file
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
    DeliveryError,
    GithubOpsError,
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
        self.clock = clock
        for hook in hooks:
            self.bus.attach_hook(hook)
        self.bus.subscribe(self._persist_event)

    def _persist_event(self, event: object) -> None:
        from sbxloop_worker.protocol import Event

        assert isinstance(event, Event)
        self.store.append_event(event)

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
        self.bus.emit(HostEventTypes.RUN_START, run_id, outcome=run.outcome, resumed=True)
        return self._drive(run_id, run.outcome)

    def cancel(self, run_id: str) -> None:
        self.store.get_run(run_id)  # raises for unknown runs
        self.store.set_run_state(run_id, "cancelled")

    # -- run driver --------------------------------------------------------

    def _drive(self, run_id: str, outcome: str) -> RunResult:
        deadline = self.clock() + self.config.budgets.max_wall_clock_s
        self._set_run_state(run_id, "provisioning")
        provisioner = Provisioner(self.sbx, self.config, self.bus)
        pair = provisioner.ensure_pair(run_id)
        assert pair.workspace is not None
        self.store.set_run_workspace(run_id, pair.workspace, pair.mounted)
        try:
            with pair:
                agent = WorkerClient(
                    pair.agent,
                    self.bus,
                    transport=self.config.worker_transport,
                    python=self.worker_python,
                )
                github = (
                    WorkerClient(
                        pair.github,
                        self.bus,
                        transport=self.config.worker_transport,
                        python=self.worker_python,
                    )
                    if pair.github is not None
                    else None
                )
                if self.install_workers:
                    # ensure_dev_tools: the agent builds projects in this VM
                    # (venvs, pip) — the github sandbox only runs API ops.
                    agent.install(extras="copilot", ensure_dev_tools=True)
                    if github is not None:
                        github.install(extras="")
                detach = self._attach_reporter(github, run_id)
                try:
                    phases = PhaseRunner(
                        agent, self.config, run_id, outcome, workdir=pair.agent_workdir
                    )
                    granter = EgressGranter(
                        self.sbx, self.config, self.bus, run_id, pair.agent.name
                    )
                    state = self._run_phases(run_id, phases, deadline, pair, granter)
                finally:
                    detach()
                    # Harvest even when a phase raised: the sandbox is still
                    # alive here, and partial artifacts beat none.
                    self._harvest(run_id, pair)
                    self._report_artifacts(run_id, pair)
                if state == "completed":
                    self._deliver(run_id, outcome, pair, github)
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
        )

    def _attach_reporter(self, github: WorkerClient | None, run_id: str) -> Callable[[], None]:
        gh = self.config.github
        if not gh.report or github is None:
            return lambda: None
        assert gh.repo is not None  # report=True without a repo cannot provision a github worker
        hook = GithubReporterHook(GithubOps(github, run_id), gh.repo)
        return self.bus.attach_hook(hook)

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
        self.bus.emit(
            HostEventTypes.RUN_ARTIFACTS,
            run_id,
            path=str(target),
            files=count,
            mounted=pair.mounted,
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
        except (DeliveryError, GithubOpsError) as exc:
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
        passed, feedback = phases.verify(task, task.plan)
        self.store.record_phase(
            run_id,
            "verify",
            task_id=task.spec.id,
            attempt=task.revisions + 1,
            status="ok" if passed else "failed",
            output_json=json.dumps({"passed": passed, "feedback": clip(feedback)}),
            started_at=started,
        )
        if passed:
            self._set_task_state(run_id, task, "validating")
        else:
            self._register_revision(run_id, task, feedback)

    def _phase_validate(
        self,
        run_id: str,
        phases: PhaseRunner,
        task: TaskRecord,
        budgets: object,
    ) -> None:
        started = time.time()
        verdict = phases.validate(task)
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

    def _register_revision(self, run_id: str, task: TaskRecord, feedback: str) -> None:
        task.revisions += 1
        task.last_feedback = feedback
        if task.revisions > self.config.budgets.max_revisions_per_task:
            self._set_task_state(run_id, task, "failed")
        else:
            self._set_task_state(run_id, task, "executing")

    # -- bookkeeping -------------------------------------------------------

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
