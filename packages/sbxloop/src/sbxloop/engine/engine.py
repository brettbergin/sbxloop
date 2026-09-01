"""LoopEngine: one run from outcome to merged pull request.

    DECOMPOSE → (BUILD → VERIFY)* → GATE → DELIVER → REVIEW ⇄ FIX → CI → LAND

The task graph is built and verified under the revision/replan budgets;
then the run gates the whole tree, delivers it as a draft PR, reviews its
own diff, spends bounded fix rounds on what the review, CI or the base
branch object to, and merges. Every stage is a run state and every
transition is checkpointed in SQLite, so a crash at any point resumes at
that stage with a fresh sandbox pair — the PR branch on GitHub is the
durable copy of the work once one exists.

Failure semantics:

- Budget exhaustion (revisions/replans) fails the *task*; dependents are
  skipped and the run ends ``failed`` before delivering anything. One
  exception: revisions exhausted by *verify-command* failures spend a
  replan first when budget remains — the builder cannot edit the
  decomposer-authored verify commands, so only a fresh session's fresh
  approach can unstick work that disagrees with where a check looks.
- Round exhaustion (``[landing] max_review_rounds`` / ``max_ci_rounds``)
  ends the run ``failed`` with the PR left open as a draft and the budget
  that ran out recorded on the run (``runs.exhausted``). The run is one
  round short, not broken: ``grant_rounds`` extends its budgets and a
  ``resume()`` then continues on the same branch and PR with the review
  history intact (#523) — the daemon does this once by itself
  (``[landing] retry_rounds``), an operator as often as they like.
- GitHub refusing to finish the PR — a protection rule, a draft that will
  not clear, CI that never reports — ends the run ``blocked``: nothing a
  further round would change, a human has to look, and the run resumes at
  ``landing`` once they have.
- Infrastructure errors (worker/sbx crashes) propagate after state is
  persisted — equivalent to a kill. ``resume()`` re-provisions a fresh
  sandbox pair (sandboxes are cattle; the workspace and SQLite state
  persist on the host) and continues from the last committed transition:
  a stage whose result was never committed re-runs from its start. Resume
  rehydrates the run's persisted config and pins the workspace from the
  runs table, so on-disk config edits (or a different cwd) cannot silently
  change the run's rules or relocate its workspace; drift is surfaced as a
  ``run.config_drift`` event.
"""

from __future__ import annotations

import json
import queue
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

from pydantic import ValidationError

from sbxloop import hostgit
from sbxloop.config import Config, RepoConfig, _flatten, load_config, load_dotenv_file
from sbxloop.deliver import deliver_workspace, ensure_repository
from sbxloop.engine.followups import (
    checklist_comment,
    collect_followups,
    issue_body,
    marker_key,
)
from sbxloop.engine.landing import (
    Blocked,
    CiTimeout,
    Closed,
    Gated,
    HumanObjection,
    Landed,
    NeedsFix,
    UpdateState,
    land,
    poll_checks,
    resolve_login,
)
from sbxloop.engine.model import (
    PIPELINE_STAGES,
    RESUMABLE_RUN_STATES,
    TERMINAL_RUN_STATES,
    FixKind,
    RunRecord,
    RunResult,
    RunState,
    SteerVerdict,
    TaskRecord,
    TaskSpec,
    TaskState,
    scan_artifacts,
)
from sbxloop.engine.phases import (
    VERIFY_FAILURE_PREFIX,
    PhaseRunner,
    VerifyFailure,
    clip,
    clip_head_tail,
    verify_suspect_feedback,
)
from sbxloop.engine.reconcile import (
    ReconcileOutcome,
    acknowledge_human_threads,
    note_nonblocking,
    post_confirmations,
    reconcile_human,
    reconcile_round,
)
from sbxloop.engine.review import (
    CarriedVerdict,
    Reconciliation,
    ReviewFinding,
    ReviewRound,
    ReviewVerdict,
    closed_anchors,
    fix_brief,
    fix_task,
    is_fix_task,
    prior_findings,
    reconcile,
    reconcile_anchor,
    render_fix_history,
    render_review_history,
    review_body,
    split_carried,
    unanswered_findings,
)
from sbxloop.engine.store import PostedRecord, StateStore
from sbxloop.errors import (
    BudgetExceededError,
    GithubOpsError,
    RunCancelledError,
    SbxError,
    SbxloopError,
    StateError,
    WorkerError,
)
from sbxloop.events import EventBus, Hook, HostEventTypes
from sbxloop.gc import workspace_pruned
from sbxloop.gh.ops import (
    FailedCheck,
    GithubOps,
    PostedFinding,
    ReviewComment,
    SubmittedReview,
    logins_match,
)
from sbxloop.ids import branch_name, new_message_id, new_run_id
from sbxloop.log import get_logger
from sbxloop.policy import EgressGranter
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.pair import SandboxPair
from sbxloop.sbx.provision import Provisioner
from sbxloop.sbx.sandbox import SBXLOOP_DIR
from sbxloop.worker.client import WorkerClient

log = get_logger(__name__)

GithubOpsFactory = Callable[[WorkerClient, str], GithubOps]


class ChatMessage(NamedTuple):
    """One queued interactive chat message, waiting for a phase boundary."""

    message_id: str
    text: str


@dataclass
class PriorArtifacts:
    """What a previous attempt at this work item pushed to origin (#600):
    the branch it delivered on and the pull request it opened. Offered to
    :meth:`LoopEngine.start`; adopted only if GitHub still has them."""

    branch: str | None = None
    pr_number: int | None = None


@dataclass
class Pipeline:
    """Everything one run's stages share while its sandbox pair is alive."""

    run_id: str
    outcome: str
    pair: SandboxPair
    phases: PhaseRunner
    granter: EgressGranter
    deadline: float
    # None when the run has no repository: the pipeline then ends after
    # the gate, `completed`.
    ops: GithubOps | None
    repo: str | None
    # The run's repository entry (per-repo deliver_base, token_env, …) with
    # the daemon-wide [github] defaults already folded in; None when the run
    # has no repository.
    repo_config: RepoConfig | None = None
    # The loop's own GitHub identity, read once and only when landing asks
    # (to tell a human's review objection from its own posted review).
    login: str | None = None
    # App mode: ``<slug>[bot]``, resolved on the host from the credential
    # itself (one cached GET /app per process); None under a PAT, where
    # GET /user answers instead. See ``_login``.
    bot_login: str | None = None
    # Whether the PR's author is that identity (#513): then GitHub refuses
    # REQUEST_CHANGES/APPROVE and the review is posted as PR comments
    # instead. Decided once per drive, on the first review round.
    self_review: bool | None = None
    # When the latest delivery happened, for the "no check runs yet" settle
    # window; None on a resume (the wait then settles from its own start).
    delivered_at: float | None = None
    fix_kinds: dict[str, FixKind] = field(default_factory=dict)
    # Human objections a `NeedsFix("human")` round is answering, held until
    # the fix re-delivers so the reply can name the sha that carries it.
    pending_human: tuple[HumanObjection, ...] = ()
    # The head commit of a previous attempt's branch this run adopted
    # (#600). The first delivery parents on it, so that attempt's commits
    # stay in the branch's history instead of being force-moved away.
    prior_head: str | None = None
    # The adopted branch itself, pinned before the first delivery so no new
    # branch name is generated; None for an ordinary run.
    branch: str | None = None
    # The previous attempt's still-open pull request, reattached to rather
    # than opening a second one for the same head.
    prior_pr: int | None = None


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
        github_ops: GithubOpsFactory | None = None,
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
        # The seam a test uses to script GitHub: every github.op the run
        # makes goes through the ops this factory returns.
        self._github_ops: GithubOpsFactory = github_ops or GithubOps
        # In-process cancellation (Ctrl-C in the TUI): checked at the same
        # phase boundaries as the store's cancelled state, but leaves the
        # persisted run state alone so the run stays resumable.
        self._cancel_event = threading.Event()
        # Set by anything that should cut a wait short — a chat message, a
        # cancel — so a poll interval never delays an answer.
        self._wake = threading.Event()
        # Seconds this run spent waiting on GitHub (CI, landing). Excluded
        # from the agent wall-clock budget: that bounds work, not waiting.
        self._waited_s = 0.0
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
        # Agent-session ids this process created, so BUILD only ever
        # resumes one that still exists. `task.session_id` is persisted, but
        # sandboxes are cattle: a resumed run gets a fresh pair and every
        # session id from the previous incarnation is dead. Membership here
        # is the difference between "this session is one turn old" and "this
        # session belonged to a VM that no longer exists".
        self._live_sessions: set[str] = set()
        # A restart's offer of the previous attempt's pushed branch/PR
        # (#600); empty for an ordinary run and for a resume.
        self._prior = PriorArtifacts()
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

    def start(
        self,
        outcome: str,
        *,
        run_id: str | None = None,
        tasks: Sequence[TaskSpec] | None = None,
        repo: str | None = None,
        prior_branch: str | None = None,
        prior_pr: int | None = None,
    ) -> RunResult:
        """Drive a fresh run all the way through.

        ``tasks`` pre-seeds the task graph and so skips DECOMPOSE — for work
        that is *already* decomposed. A normal run passes nothing.

        ``prior_branch``/``prior_pr`` are what a previous attempt at this
        work item left on the GitHub origin (#600). A restart offers them
        here; the run adopts them once GitHub confirms the branch is still
        there and still related to the base branch, and otherwise starts
        fresh with a logged reason. Nothing here can fail the run.

        ``repo`` is the ``owner/name`` this run belongs to — the repository
        its work item came from. It narrows the engine's GitHub config to
        that one repository for the whole run (and is persisted with the
        run, so a resume routes there too); ``None`` keeps the configured
        default, which is the only repository when just one is configured.
        """
        run_id = run_id or new_run_id()
        self._select_repo(repo)
        self.store.create_run(run_id, outcome, self.config.model_dump_json())
        if tasks:
            self.store.save_tasks(run_id, list(tasks))
        self.bus.emit(HostEventTypes.RUN_START, run_id, outcome=outcome, seeded=len(tasks or ()))
        self._prior = PriorArtifacts(branch=prior_branch, pr_number=prior_pr)
        return self._drive(run_id, outcome)

    def _select_repo(self, repo: str | None) -> None:
        """Pin this engine's GitHub config to the run's repository.

        Narrowing happens before the run row is written, so the persisted
        config carries the one repository and a resume — which rehydrates
        that config — routes every GitHub call to the same place. An unknown
        selector is a configuration error, not a silent delivery elsewhere.
        """
        if repo is not None and self.config.github.find_repo(repo) is None:
            known = ", ".join(r.repo for r in self.config.github.repo_list()) or "none"
            raise StateError(f"repository {repo!r} is not configured (configured: {known})")
        github = self.config.github.for_repo(repo, workspace=self.config.workspace_for_repo(repo))
        self.config = self.config.model_copy(update={"github": github})

    def resume(self, run_id: str) -> RunResult:
        """Continue a run from the last stage it committed.

        A run interrupted before it delivered anything re-enters its task
        graph; one interrupted afterwards re-enters the pipeline stage it
        was in (``runs.stage``), on a fresh sandbox pair that cloned its PR
        branch — so a crash during a CI wait costs a re-poll, not a rebuild.
        """
        run = self.store.get_run(run_id)
        if run.state not in RESUMABLE_RUN_STATES:
            raise StateError(f"run {run_id} is {run.state}; only unfinished runs can resume")
        if run.exhausted is not None:
            # Resuming as-is would spend a whole review round only to
            # re-exhaust at the first request for changes.
            raise StateError(
                f"run {run_id} exhausted its {run.exhausted} fix rounds; grant more first "
                f"(`sbxloop resume {run_id} --grant-rounds N`, or `sbxloop daemon ctl "
                f"grant-rounds {run_id} N` under the daemon)"
            )
        self._refuse_if_pruned(run_id)
        stage = run.stage or run.state
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
            # The reason belonged to the attempt that stopped; a resumed run
            # earns its own or ends merged.
            self.store.set_run_reason(run_id, None)
        self._rehydrate_config(run_id)
        self.bus.emit(HostEventTypes.RUN_START, run_id, outcome=run.outcome, resumed=True)
        return self._drive(run_id, run.outcome, workspace=run.workspace, stage=stage)

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
        self._wake.set()

    def request_cancel(self) -> None:
        """Ask a running engine (from another thread) to stop at the next
        phase boundary. In-process only: unlike ``cancel`` it does not touch
        the persisted run state, so the interrupted run remains resumable."""
        self._cancel_event.set()
        self._wake.set()

    def post_user_message(self, text: str) -> str:
        """Queue an interactive chat message for the run this engine is
        driving. Thread-safe; returns the message id. The agent pauses at
        the next phase boundary — or, during a CI or landing wait, at once —
        answers over a read-only STEER session, and applies any course
        change the reply calls for.
        """
        message = ChatMessage(new_message_id(), text)
        self._chat_queue.put(message)
        self._wake.set()
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
        # `github.enabled_repo_count` is bookkeeping recorded when a run's
        # config was narrowed to its repository, not an operator setting: it
        # differs from the live config by construction and says nothing about
        # drift.
        ignore = {"github.enabled_repo_count"}
        return [
            f"{key} (run: {stored_flat.get(key)!r}, current: {current_flat.get(key)!r})"
            for key in sorted(stored_flat.keys() | current_flat.keys())
            if key not in ignore and stored_flat.get(key) != current_flat.get(key)
        ]

    # -- run driver --------------------------------------------------------

    def _drive(
        self,
        run_id: str,
        outcome: str,
        *,
        workspace: Path | None = None,
        stage: str | None = None,
    ) -> RunResult:
        self._waited_s = 0.0
        deadline = self.clock() + self.config.budgets.max_wall_clock_s
        self._set_run_state(run_id, "provisioning")
        # A restart pins its clone to the branch the previous attempt pushed
        # BEFORE the workspace is cut (#600), so the agent starts from that
        # work, the review diff describes it, and the delivered tree is the
        # one the agent actually built. Pinning after provisioning would
        # only change where the result lands.
        provisioner = Provisioner(self.sbx, self._provision_config(), self.bus)
        # A resumed run's workspace is pinned from the runs table — never
        # recomputed from config, which would silently relocate it (#60).
        # The run's repository (its config was narrowed to it in
        # _select_repo) scopes the github sandbox's token and remote.
        pair = provisioner.ensure_pair(run_id, workspace, self.config.github.repo)
        assert pair.workspace is not None
        self._confirm_prior_checkout(run_id, pair)
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
        state: RunState
        reason: str | None
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
                        # Per-job stdin delivery when provisioning chose it;
                        # None keeps the launch exactly as before (#592).
                        job_env=provisioner.job_env("agent", sandbox=pair.agent),
                    )
                    github = (
                        WorkerClient(
                            pair.github,
                            self.bus,
                            transport=self.config.worker_transport,
                            python=self.worker_python,
                            role="github",
                            limits=self.config.limits,
                            # App auth: keep the installation token fresh for
                            # every github op; None under a PAT — and under
                            # stdin delivery, where job_env re-mints per job.
                            credential_refresh=provisioner.gh_refresher(
                                pair.github, self.config.github.repo
                            ),
                            job_env=provisioner.job_env(
                                "github", self.config.github.repo, sandbox=pair.github
                            ),
                        )
                        if pair.github is not None
                        else None
                    )
                    if self.install_workers:
                        self._install_workers(run_id, pair, agent, github)
                    repo_config = self.config.github.effective_repo(None)
                    ops = (
                        self._github_ops(github, run_id)
                        if github is not None and repo_config is not None
                        else None
                    )
                    self._ensure_delivery_repo(run_id, ops)
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
                    pipeline = Pipeline(
                        run_id=run_id,
                        outcome=outcome,
                        pair=pair,
                        phases=phases,
                        granter=EgressGranter(
                            self.sbx, self.config, self.bus, run_id, pair.agent.name
                        ),
                        deadline=deadline,
                        ops=ops,
                        repo=repo_config.repo if ops is not None and repo_config else None,
                        repo_config=repo_config if ops is not None else None,
                        bot_login=(
                            provisioner.gh_bot_login(self.config.github.repo)
                            if ops is not None
                            else None
                        ),
                    )
                    try:
                        self._adopt_prior_artifacts(pipeline)
                        state, reason = self._run_pipeline(pipeline, stage)
                    finally:
                        # Harvest even when a stage raised: the sandbox is
                        # still alive here, and partial artifacts beat none.
                        self._harvest(run_id, pair)
                        self._report_artifacts(run_id, pair)
                except SbxloopError:
                    # Infra failures (install, worker, sbx) are exactly what
                    # gets diagnosed in-sandbox; decide keep before pair exit.
                    self._keep_on_failure(run_id, pair)
                    raise
                if state not in ("merged", "completed", "gated"):
                    self._keep_on_failure(run_id, pair)
        except SbxloopError:
            # State is already persisted; the exception is the kill signal.
            raise
        if reason:
            self.store.set_run_reason(run_id, reason)
        self._set_run_state(run_id, state)
        run = self.store.get_run(run_id)
        tasks = self.store.get_tasks(run_id)
        self.bus.emit(
            HostEventTypes.RUN_END,
            run_id,
            state=state,
            reason=reason,
            pr=run.pr_number,
            url=run.pr_url,
            exhausted=run.exhausted,
        )
        return RunResult(
            run_id=run_id,
            state=state,
            exhausted=run.exhausted,
            tasks=tasks,
            workspace=pair.workspace,
            mounted=pair.mounted,
            kept_sandboxes=self._pair_names(pair) if pair.keep else [],
            pr_number=run.pr_number,
            pr_url=run.pr_url,
            reason=reason,
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
                # The backend name doubles as the worker extra ([copilot] /
                # [claude]); the claude extra also ensures the Claude Code
                # CLI runtime (#533).
                extras=self.config.agent.backend,
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

    def _artifact_source(self, run_id: str, pair: SandboxPair) -> Path | None:
        target = (
            pair.workspace
            if pair.mounted
            else self.config.state_dir / "runs" / run_id / "artifacts"
        )
        return target if target is not None and target.is_dir() else None

    def _report_artifacts(self, run_id: str, pair: SandboxPair) -> None:
        target = self._artifact_source(run_id, pair)
        if target is None:
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

    def _ensure_delivery_repo(self, run_id: str, ops: GithubOps | None) -> None:
        """Probe (and, when allowed, create) the delivery repo up front.

        Runs right after worker install so a missing or typo'd repository
        fails the run before any planning or execution happens, not after
        the work is done. A creation is surfaced as a run.deliver event so
        the transcript records where the artifacts will land.
        """
        entry = self.config.github.effective_repo(None)
        if ops is None or entry is None:
            return
        created = ensure_repository(
            ops, entry.repo, create=entry.create_repo, public=entry.create_public
        )
        if created:
            self.bus.emit(HostEventTypes.RUN_DELIVER, run_id, repo=entry.repo, created=True)

    def _provision_config(self) -> Config:
        """The config provisioning sees: a restart's offered branch pinned
        as ``sandbox.continue_branch`` so the run's clone is cut from the
        previous attempt's work rather than from the base branch (#600).

        The pin is *optional* — unlike a resume, a restart has published
        nothing of its own, so a branch that is gone from origin is a fresh
        start with a logged reason, not a failed provision.
        """
        branch = self._prior.branch
        if not branch:
            return self.config
        sandbox = self.config.sandbox.model_copy(
            update={"continue_branch": branch, "continue_branch_optional": True}
        )
        return self.config.model_copy(update={"sandbox": sandbox})

    def _confirm_prior_checkout(self, run_id: str, pair: SandboxPair) -> None:
        """Keep the branch offer only if the workspace really landed on it.

        Provisioning may have fallen back to a fresh cut (the branch was
        deleted on origin, the checkout could not fetch it) or reused an
        existing clone. Adopting the branch for *delivery* in that case is
        exactly the union-tree bug this pinning exists to remove: the run
        would push a tree diffed against base onto a branch whose history
        it never contained. Dropping the offer here makes the workspace,
        the review diff and the delivered tree describe one history.
        """
        branch = self._prior.branch
        if not branch:
            return
        workspace = pair.workspace
        if workspace is None or hostgit.repo_toplevel(workspace) is None:
            # Not a git checkout at all (an in-place plain directory): there
            # was no branch to pin and delivery is a snapshot, so the offer
            # is still just "land it on that branch, keeping its history".
            return
        actual = hostgit.current_branch(workspace)
        if actual == branch:
            return
        log.info(
            "engine.prior_branch_unusable",
            run=run_id,
            branch=branch,
            reason=(
                f"the run workspace is on {actual or 'no branch'}, not the offered "
                "branch; starting fresh from the base branch"
            ),
        )
        self._prior = PriorArtifacts()

    def _adopt_prior_artifacts(self, p: Pipeline) -> None:
        """Continue on what a previous attempt at this item pushed (#600).

        A restart offers the branch (and PR) of the attempt before it. This
        confirms with GitHub that the branch is still on origin and still
        related to the base branch — a shared merge base — and only then
        pins the run to it: the delivery lands on that branch, parented on
        its head, so the earlier commits stay in the history, and the open
        pull request for that head is refreshed rather than a second one
        opened.

        Anything unusable — no branch, an unrelated/force-diverged branch,
        a PR that is closed or merged, a GitHub call that fails — is a
        fresh start with one ``engine.prior_branch_unusable`` line carrying
        the reason. This never raises: a restart that cannot reuse work is
        still a perfectly good run.
        """
        prior, ops, repo = self._prior, p.ops, p.repo
        if prior.branch is None:
            return
        branch = prior.branch
        if ops is None or repo is None:
            self._prior_unusable(p, branch, "the run has no GitHub repository")
            return
        base = p.repo_config.deliver_base if p.repo_config else self.config.github.deliver_base
        try:
            if base is None:
                base = str(ops.repo_get(repo).get("default_branch") or "main")
            head_sha = ops.ref_lookup(repo, f"heads/{branch}")
            if head_sha is None:
                self._prior_unusable(p, branch, "the branch is no longer on origin")
                return
            if not self._shares_merge_base(ops, repo, base, branch):
                self._prior_unusable(
                    p, branch, f"the branch has no merge base with {base} (unrelated history)"
                )
                return
            pr_number = self._prior_open_pr(ops, repo, branch, prior.pr_number)
        except (GithubOpsError, SbxloopError) as exc:
            self._prior_unusable(p, branch, f"GitHub could not confirm it: {exc}")
            return
        p.branch, p.prior_head, p.prior_pr = branch, head_sha, pr_number
        log.info(
            "engine.prior_branch_reused",
            run=p.run_id,
            repo=repo,
            branch=branch,
            head=head_sha[:12],
            pr=pr_number,
            base=base,
        )
        self.bus.emit(
            HostEventTypes.RUN_DELIVER,
            p.run_id,
            repo=repo,
            branch=branch,
            head_sha=head_sha,
            pr=pr_number,
            reused=True,
            message=f"continuing the previous attempt's branch {branch}",
        )

    def _prior_unusable(self, p: Pipeline, branch: str, reason: str) -> None:
        """Say once, structured, why a restart is starting fresh (#600)."""
        log.info(
            "engine.prior_branch_unusable",
            run=p.run_id,
            repo=p.repo,
            branch=branch,
            reason=reason,
        )

    @staticmethod
    def _shares_merge_base(ops: GithubOps, repo: str, base: str, branch: str) -> bool:
        """Whether ``branch`` and ``base`` have a common ancestor — the test
        for "this branch is still about this repository's current line of
        work". A comparison GitHub cannot make (404 on unrelated histories)
        answers no rather than raising."""
        try:
            data = ops.raw("GET", f"/repos/{repo}/compare/{base}...{branch}")
        except GithubOpsError as exc:
            if exc.http_status == 404:
                return False
            raise
        if not isinstance(data, dict):
            return False
        merge_base = data.get("merge_base_commit")
        return bool(isinstance(merge_base, dict) and merge_base.get("sha"))

    @staticmethod
    def _prior_open_pr(ops: GithubOps, repo: str, branch: str, recorded: int | None) -> int | None:
        """The open pull request for ``branch``, or None when there is none
        to reattach to (it was closed or merged, so the restart opens a
        fresh one on the same branch)."""
        owner = repo.split("/", 1)[0]
        pulls = ops.raw("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}")
        if isinstance(pulls, list):
            for pull in pulls:
                if isinstance(pull, dict) and pull.get("number"):
                    return int(pull["number"])
        if recorded is None:
            return None
        data = ops.pr_get(repo, recorded)
        if data.get("state") == "open" and not data.get("merged"):
            return recorded
        return None

    # -- the pipeline ------------------------------------------------------

    def _run_pipeline(self, p: Pipeline, stage: str | None) -> tuple[RunState, str | None]:
        """Drive the run from ``stage`` (None: the beginning) to a terminal
        state; returns it with the reason the run stopped short of merged."""
        state, reason = self._stages(p, stage)
        # A message that arrived during the last wait or stage still gets
        # answered — as steer_run; there is nothing left to steer.
        self._process_chat(p.run_id, p.phases, None, stage=f"finished ({state})")
        return state, reason

    def _stages(self, p: Pipeline, stage: str | None) -> tuple[RunState, str | None]:
        if stage not in PIPELINE_STAGES:
            failed = self._run_phases(p)
            if failed:
                return "failed", self._failure_reason(p.run_id)
            stage = "gating"
        # Every fix round returns to the gate: it is one cheap shell batch
        # and the guarantee that a red tree is never delivered, whatever the
        # round was for.
        while True:
            self._check_cancelled_and_clock(p.run_id, p.deadline)
            if stage == "gating":
                reason = self._stage_gate(p)
                if reason is not None:
                    return "failed", reason
                if p.ops is None:
                    return "completed", None
                stage = "delivering"
            elif stage == "delivering":
                self._stage_deliver(p)
                self._stage_reconcile(p)
                self._stage_reconcile_human(p)
                stage = "reviewing"
            elif stage == "reviewing":
                verdict = self._stage_review(p)
                if verdict.verdict == "approve":
                    stage = "awaiting_ci"
                    continue
                # Every finding rides into the brief (#522): blocking ones to
                # be addressed or refuted, the rest to be answered — addressed,
                # refuted or deferred — rather than silently dropped.
                reason = self._fix_round(
                    p, "review", "the review requested changes", findings=verdict.findings
                )
                if reason is not None:
                    return "failed", reason
                stage = "gating"
            elif stage == "fixing":
                # Resume mid fix round: finish the task that was in flight.
                reason = self._resume_fix(p)
                if reason is not None:
                    return "failed", reason
                stage = "gating"
            elif stage == "awaiting_ci":
                result = self._stage_ci(p)
                if isinstance(result, Blocked):
                    return "blocked", result.why
                if isinstance(result, NeedsFix):
                    reason = self._fix_round(
                        p, result.kind, result.why, failed_checks=result.failed_checks
                    )
                    if reason is not None:
                        return "failed", reason
                    stage = "gating"
                    continue
                stage = "landing"
            elif stage == "landing":
                outcome = self._stage_land(p)
                if isinstance(outcome, Landed):
                    return "merged", None
                if isinstance(outcome, Gated):
                    return "gated", (
                        "parked by [landing] merge_gate — ready to merge, awaiting human approval"
                    )
                if isinstance(outcome, Blocked):
                    return "blocked", outcome.why
                if isinstance(outcome, Closed):
                    return "failed", outcome.why
                reason = self._fix_round(
                    p,
                    outcome.kind,
                    outcome.why,
                    failed_checks=outcome.failed_checks,
                    objections=outcome.objections,
                    human=outcome.human,
                )
                if reason is not None:
                    return "failed", reason
                stage = "gating"
            else:  # pragma: no cover - defensive
                raise StateError(f"run {p.run_id} in unexpected stage {stage!r}")

    def _failure_reason(self, run_id: str) -> str:
        """Why the task graph stopped, naming a suspect check when there is one.

        A verify command that failed identically every attempt is not the
        same failure as work that could not be done, and #387 is precisely
        about that difference reaching a human instead of being spent as
        another silent retry. The builder cannot re-author the command, so
        the run outcome is where the diagnosis has to surface.
        """
        suspects = [
            t
            for t in self.store.get_tasks(run_id)
            if t.state == "failed" and t.verify_suspect and t.spec.verify_commands
        ]
        if suspects:
            task = suspects[0]
            commands = ", ".join(f"`{c}`" for c in task.spec.verify_commands)
            return (
                f"task {task.spec.id} failed a verify command that never changed "
                f"its result across attempts — the check looks unpassable and "
                f"needs re-authoring: {commands}"
            )
        return "a task failed or was skipped"

    def _run_phases(self, p: Pipeline) -> bool:
        """DECOMPOSE (unless seeded or resumed) and the task graph; True when
        a task failed or was skipped."""
        run_id, phases = p.run_id, p.phases
        tasks = [t for t in self.store.get_tasks(run_id) if not is_fix_task(t.spec.id)]
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
            spend = phases.drain_spend()
            self.store.record_phase(
                run_id,
                "decompose",
                task_id=None,
                attempt=1,
                status="ok",
                output_json=graph.model_dump_json(),
                started_at=started,
                usage=spend.usage,
                turns=spend.turns,
            )
            tasks = self.store.get_tasks(run_id)

        self._set_run_state(run_id, "building")
        self._announce_roster(run_id, tasks)
        failed_ids, skipped_ids = self._schedule_tasks(p, tasks)
        # Messages that arrived during the last phase still get answered
        # (as steer_run — there is no task left to steer).
        self._process_chat(run_id, phases, None, stage="between the task graph and the gate")
        return bool(failed_ids or skipped_ids)

    def _announce_roster(self, run_id: str, tasks: Sequence[TaskRecord]) -> None:
        # Announce the full roster up front (with titles) so UIs can show
        # every task waiting immediately, instead of revealing rows one at a
        # time as each prior task finishes. Also runs on resume, where it
        # restores the table with each task's persisted state, and after a
        # fix task is appended.
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

    def _schedule_tasks(
        self, p: Pipeline, tasks: Sequence[TaskRecord]
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
        run_id = p.run_id
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
                    running[pool.submit(self._run_task, p, task)] = task
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

    # -- post-build stages -------------------------------------------------

    def _stage_gate(self, p: Pipeline) -> str | None:
        """Run the project's own gate over the whole tree before delivering.

        The decomposer must put the gate in *some* task's verify commands,
        but a later task can break what an earlier one proved; this is the
        run's last mechanical check, on the tree exactly as it will be
        delivered. A red gate spends a fix round on the CI budget — it is
        the round red CI would have cost, caught before GitHub's compute.
        Returns the reason the run failed, or None to continue.
        """
        run_id, phases = p.run_id, p.phases
        self._set_run_state(run_id, "gating")
        gate = phases.project_gate()
        attempt = 1 + sum(1 for row in self.store.phase_attempts(run_id) if row["phase"] == "gate")
        started = time.time()
        if not gate:
            self.store.record_phase(
                run_id,
                "gate",
                task_id=None,
                attempt=attempt,
                status="skipped",
                output_json=json.dumps({"reason": "the project declares no gate"}),
                started_at=started,
            )
            return None
        result = phases.shell_batch([gate])[0]
        passed = result.exit_code == 0
        output = clip_head_tail(result.output)
        self.store.record_phase(
            run_id,
            "gate",
            task_id=None,
            attempt=attempt,
            status="ok" if passed else "failed",
            output_json=json.dumps(
                {"command": gate, "exit_code": result.exit_code, "output": output}
            ),
            started_at=started,
        )
        first_line = next((line for line in output.splitlines() if line.strip()), "")
        self.bus.emit(
            HostEventTypes.PHASE_END,
            run_id,
            task_id=None,
            phase="gate",
            status="ok" if passed else "failed",
            attempt=attempt,
            message=f"`{gate}` passed"
            if passed
            else f"`{gate}` exit {result.exit_code}: {first_line}",
        )
        if passed:
            return None
        reason = self._fix_round(
            p,
            "gate",
            f"the project gate `{gate}` failed (exit {result.exit_code})",
            failed_checks=(FailedCheck(gate, "failure", output, ""),),
        )
        if reason is not None:
            return reason
        # The fix is in; the gate is the judge of that, so it runs again
        # (bounded: every round spends the CI budget).
        return self._stage_gate(p)

    def _stage_deliver(self, p: Pipeline) -> None:
        """Open the pull request, or refresh it: the same branch every round.

        Errors propagate — the run is resumable at ``delivering`` and a
        resume re-delivers, which is idempotent (a branch that exists is
        force-moved, an open PR is reused).
        """
        run_id, ops, repo = p.run_id, p.ops, p.repo
        assert ops is not None and repo is not None
        self._set_run_state(run_id, "delivering")
        run = self.store.get_run(run_id)
        # Unmounted runs deliver the harvest; refresh it so the fix round's
        # writes are in it.
        self._harvest(run_id, p.pair)
        source = self._artifact_source(run_id, p.pair)
        if source is None:
            raise StateError(f"run {run_id} has no artifacts directory to deliver")
        gh, landing = self.config.github, self.config.landing
        branch = run.branch or p.branch or branch_name(run_id)
        # Only the first delivery of an adopted branch continues its
        # history; once this run has delivered, later rounds force-move
        # onto their own head as they always did.
        parent = p.prior_head if run.branch is None else None
        round_no = 1 + sum(1 for t in self.store.get_tasks(run_id) if is_fix_task(t.spec.id))
        started = time.monotonic()
        log.info(
            "run.deliver_start",
            run=run_id,
            repo=repo,
            branch=branch,
            round=round_no,
            draft=landing.deliver_draft,
        )
        pr = deliver_workspace(
            ops,
            repo,
            run_id=run_id,
            outcome=p.outcome,
            source_dir=source,
            base=(p.repo_config.deliver_base if p.repo_config else gh.deliver_base),
            draft=landing.deliver_draft,
            exclude=self.config.artifacts.exclude,
            branch=branch,
            closes=gh.deliver_closes,
            pr_number=run.pr_number if run.pr_number is not None else p.prior_pr,
            round_no=round_no,
            parent=parent,
        )
        data = ops.pr_get(repo, pr.number)
        head = data.get("head")
        head_sha = str(head.get("sha")) if isinstance(head, dict) and head.get("sha") else None
        self.store.set_run_pr(
            run_id,
            number=pr.number,
            url=pr.url,
            branch=branch,
            head_sha=head_sha,
            node_id=str(data["node_id"]) if data.get("node_id") else None,
        )
        if run.pr_number is None and p.prior_pr is None:
            self._label_pr(p, pr.number)
        p.delivered_at = self.clock()
        log.info(
            "run.delivered",
            run=run_id,
            repo=repo,
            pr=pr.number,
            url=pr.url,
            head=head_sha,
            duration_s=round(time.monotonic() - started, 1),
        )
        self.bus.emit(
            HostEventTypes.RUN_DELIVER,
            run_id,
            repo=repo,
            pr=pr.number,
            url=pr.url,
            branch=branch,
            head_sha=head_sha,
            round=round_no,
        )

    def _label_pr(self, p: Pipeline, number: int) -> None:
        """Put the repository's own ``labels`` (``[[github.repos]]``) on the
        pull request the run just opened — the other half of what the config
        promises, the issue half being applied at claim. Best effort: a
        label refusal must not fail a delivery that succeeded."""
        labels = list(p.repo_config.labels) if p.repo_config is not None else []
        if not labels or p.ops is None or p.repo is None:
            return
        try:
            p.ops.raw("POST", f"/repos/{p.repo}/issues/{number}/labels", {"labels": labels})
        except GithubOpsError:
            log.warning(
                "deliver.pr_labels_failed", run=p.run_id, pr=number, labels=labels, exc_info=True
            )

    def _stage_review(self, p: Pipeline) -> ReviewVerdict:
        """The run's own adversarial review of its PR. The verdict is ours;
        it is posted to the PR for the record."""
        run_id, phases, ops, repo = p.run_id, p.phases, p.ops, p.repo
        assert ops is not None and repo is not None
        self._set_run_state(run_id, "reviewing")
        run = self.store.get_run(run_id)
        assert run.pr_number is not None
        self._process_chat(run_id, phases, None, stage=f"reviewing PR #{run.pr_number}")
        rounds = self._review_rounds(run_id)
        round_no = len(rounds) + 1
        diff = self._diff_for_review(p, run.head_sha)
        started = time.time()
        verdict = phases.review(
            diff=diff,
            pr_number=run.pr_number,
            round=round_no,
            tasks=self.store.get_tasks(run_id),
            history=render_review_history(rounds),
            refuted=closed_anchors(rounds),
        )
        # Round n+1's word on a finding an earlier round raised belongs in
        # that finding's own thread, not restated in a fresh review body
        # (#520 step 4). ``posting`` is the body/inline half — summary plus
        # genuinely new findings; ``carried`` is replied in-thread below.
        prior = prior_findings(rounds)
        posting, carried = split_carried(verdict, prior)
        # The run acts on the union: new findings plus the carried ones this
        # round says are still open, so "still open" keeps driving fix rounds.
        # Read off ``posting``: a finding the reviewer re-filed on an old
        # anchor is a carried still-open there, and only there (#522).
        verdict = posting.model_copy(
            update={"findings": [*posting.findings, *posting.carried_forward(prior)]}
        )
        posted_url = ""
        posted_event = ""
        posted_findings: tuple[PostedFinding, ...] = ()
        posted_review_id: int | None = None
        comments = posting.comments()
        try:
            try:
                if self._self_review(p, run.pr_number):
                    submitted = self._post_review_as_comments(
                        p, run.pr_number, run.head_sha, posting, comments, round_no
                    )
                else:
                    submitted = ops.pr_review_create(
                        repo,
                        run.pr_number,
                        verdict.event,
                        review_body(posting, run_id=run_id, round=round_no),
                        comments,
                    )
            except GithubOpsError:
                if not comments or p.self_review:
                    raise
                # A finding anchored to a line outside the diff makes GitHub
                # refuse the whole review (422), in both the requested event
                # and the COMMENT fallback. The findings matter more than
                # their anchors: post them in the body instead.
                log.warning(
                    "review.post_inline_refused",
                    run=run_id,
                    pr=run.pr_number,
                    comments=len(comments),
                    hint="re-posting the review with its findings in the body",
                )
                submitted = ops.pr_review_create(
                    repo,
                    run.pr_number,
                    verdict.event,
                    review_body(posting, run_id=run_id, round=round_no, anchored=False),
                )
            posted_url, posted_event = submitted.url, submitted.event
            posted_review_id = submitted.review_id
            # Every finding is accounted for, thread or not: the ones the
            # review posted inline keep their comment/thread ids, the rest
            # (no line, over the cap, or an anchor GitHub refused) are
            # recorded body-only so reconciliation can still speak to them.
            captured = {p.anchor: p for p in submitted.posted}
            posted_findings = tuple(
                captured.get(f.anchor, PostedFinding(f.anchor)) for f in posting.findings
            )
            # A carried finding still open keeps the thread it already has,
            # so the fix round that follows reconciles onto it rather than
            # into a body comment.
            posted_findings += tuple(
                PostedFinding(rec.anchor, rec.comment_id, rec.thread_node_id)
                for rec in self._threads_for(run_id, [c.anchor for c in carried if not c.fixed])
            )
        except GithubOpsError:
            # No longer a courtesy (#520 step 5): the review record *is* the
            # PR's audit trail, and a round that could not post one leaves
            # `review.url` empty — which `land()` reads as a merge block
            # rather than merging silently (#503).
            log.error("review.post_failed", run=run_id, pr=run.pr_number, exc_info=True)
        self._post_confirmations(p, round_no, carried)
        # An approving round's own `minor`/`nit` findings each opened a
        # thread that no fix round will ever answer — there is no fix round
        # after an approval. Answer them here, or the merge gate blocks on
        # threads nothing in the pipeline can reconcile.
        if verdict.verdict == "approve":
            self._note_nonblocking(p, round_no, posted_findings, posting)
        spend = phases.drain_spend()
        self.store.record_phase(
            run_id,
            "review",
            task_id=None,
            attempt=round_no,
            status=verdict.verdict,
            output_json=json.dumps(
                {
                    "verdict": verdict.model_dump(),
                    "review": {
                        "url": posted_url,
                        "event": posted_event,
                        "id": posted_review_id,
                    },
                    "posted": [p._asdict() for p in posted_findings],
                }
            ),
            started_at=started,
            usage=spend.usage,
            turns=spend.turns,
        )
        self.store.set_run_verdict(run_id, verdict.verdict)
        self.bus.emit(
            HostEventTypes.REVIEW_VERDICT,
            run_id,
            pr=run.pr_number,
            round=round_no,
            verdict=verdict.verdict,
            findings=len(verdict.findings),
            blocking=len(verdict.blocking),
            url=posted_url,
            posted_event=posted_event,
            summary=" ".join(verdict.summary.split())[:300],
        )
        return verdict

    def _self_review(self, p: Pipeline, number: int) -> bool:
        """Is the loop reviewing a PR it authored? (#513)

        One token opens the PR and reviews it, so the answer is yes on
        every daemon run today; a second, reviewer-only identity would make
        it no. GitHub refuses ``REQUEST_CHANGES`` and ``APPROVE`` from a
        PR's own author, so asking is two doomed calls per round. Decided
        once per drive from the PR's author; unknown reads as no, which
        keeps the review-feature path (and its COMMENT fallback).
        """
        if p.self_review is None:
            assert p.ops is not None and p.repo is not None
            author = ""
            try:
                user = p.ops.pr_get(p.repo, number).get("user")
                author = str(user.get("login") or "") if isinstance(user, dict) else ""
            except GithubOpsError:
                log.warning("review.author_lookup_failed", run=p.run_id, pr=number, exc_info=True)
            login = self._login(p)
            p.self_review = logins_match(author, login)
            if p.self_review:
                log.info(
                    "review.self_review",
                    run=p.run_id,
                    pr=number,
                    login=login,
                    hint="the loop authored this PR; GitHub refuses REQUEST_CHANGES/APPROVE "
                    "from an author, so the review is posted as PR comments — one thread "
                    "per finding, the verdict in a top-level comment",
                )
        return bool(p.self_review)

    def _post_review_as_comments(
        self,
        p: Pipeline,
        number: int,
        head_sha: str | None,
        posting: ReviewVerdict,
        comments: Sequence[ReviewComment],
        round_no: int,
    ) -> SubmittedReview:
        """The single-identity review (#513): each anchored finding as its
        own review comment (a resolvable thread), then the verdict and every
        finding that got no thread — no line, over the cap, or an anchor
        GitHub refused — in one top-level PR comment."""
        ops, repo, run_id = p.ops, p.repo, p.run_id
        assert ops is not None and repo is not None
        posted: tuple[PostedFinding, ...] = ()
        if comments and head_sha:
            posted = ops.pr_review_comments_create(repo, number, comments, commit_id=head_sha)
        threaded = {rec.anchor for rec in posted if rec.comment_id is not None}
        in_body = [f for f in posting.findings if f.anchor not in threaded]
        url = ops.pr_issue_comment(
            repo, number, review_body(posting, run_id=run_id, round=round_no, in_body=in_body)
        )
        return SubmittedReview(url, "COMMENT", None, posted)

    def _threads_for(self, run_id: str, anchors: Sequence[str]) -> list[PostedRecord]:
        """The thread identity earlier rounds recorded for these anchors."""
        wanted = list(dict.fromkeys(anchors))
        if not wanted:
            return []
        best: dict[str, PostedRecord] = {}
        for rec in self.store.posted_findings(run_id):
            if rec.anchor in wanted and not rec.body_only:
                best[rec.anchor] = rec
        return [best[a] for a in wanted if a in best]

    def _post_confirmations(
        self, p: Pipeline, round_no: int, carried: Sequence[CarriedVerdict]
    ) -> None:
        """Reply, in each carried-over finding's own thread, with this round's
        verdict on it — and resolve the ones confirmed fixed (#520 step 4)."""
        run_id, ops, repo = p.run_id, p.ops, p.repo
        if ops is None or repo is None or not carried:
            return
        run = self.store.get_run(run_id)
        if run.pr_number is None:
            return
        try:
            outcome = post_confirmations(
                ops,
                repo,
                run.pr_number,
                run_id=run_id,
                round=round_no,
                items=carried,
                posted=self.store.posted_findings(run_id),
                done=self.store.confirmations(run_id, round_no),
                record=partial(self._record_confirmation, run_id, round_no),
            )
        except GithubOpsError:
            log.warning("review.confirm_failed", run=run_id, pr=run.pr_number, exc_info=True)
            return
        if not outcome.did_anything:
            return
        self.bus.emit(
            HostEventTypes.REVIEW_RECONCILED,
            run_id,
            pr=run.pr_number,
            round=round_no,
            addressed=outcome.confirmed,
            refuted=0,
            unanswered=outcome.still_open,
            replied=outcome.replied,
            resolved=outcome.resolved,
            body_only=outcome.body_only,
            comment_url="",
            confirmations=len(carried),
        )

    def _record_confirmation(
        self, run_id: str, round: int, *, anchor: str, status: str, resolved: bool
    ) -> None:
        self.store.record_confirmation(run_id, round, anchor, status, resolved=resolved)

    def _record_noted(
        self, run_id: str, round: int, *, anchor: str, status: str, resolved: bool
    ) -> None:
        self.store.record_noted(run_id, round, anchor, status, resolved=resolved)

    def _note_nonblocking(
        self,
        p: Pipeline,
        round_no: int,
        posted: Sequence[PostedFinding],
        verdict: ReviewVerdict,
    ) -> None:
        """Reconcile an approving round's findings in-thread.

        `approve` ends the review stage — no fix round follows, so nothing
        else would ever speak to the threads this round's findings opened,
        and `land()`'s reconciliation gate would block the merge on them
        forever. Which findings need an answer is decided by reachability,
        not severity: an `approve` may carry a `major`, and that finding
        gets an inline thread like any other. Each gets a reply worded for
        its severity and is resolved.
        """
        run_id, ops, repo = p.run_id, p.ops, p.repo
        if ops is None or repo is None:
            return
        severities = {f.anchor: f.severity for f in verdict.findings}
        if not severities:
            return
        run = self.store.get_run(run_id)
        if run.pr_number is None:
            return
        records = [PostedRecord(round_no, f.anchor, f.comment_id, f.thread_node_id) for f in posted]
        try:
            outcome = note_nonblocking(
                ops,
                repo,
                run.pr_number,
                run_id=run_id,
                round=round_no,
                findings=severities,
                posted=records,
                done=self.store.noted(run_id, round_no),
                record=partial(self._record_noted, run_id, round_no),
            )
        except GithubOpsError:
            log.warning("review.noted_failed", run=run_id, pr=run.pr_number, exc_info=True)
            return
        if not outcome.did_anything:
            return
        self.bus.emit(
            HostEventTypes.REVIEW_RECONCILED,
            run_id,
            pr=run.pr_number,
            round=round_no,
            addressed=0,
            refuted=0,
            unanswered=0,
            replied=outcome.replied,
            resolved=outcome.resolved,
            body_only=outcome.body_only,
            comment_url="",
            noted=outcome.noted,
        )

    def _stage_reconcile(self, p: Pipeline) -> None:
        """Speak each closed review round's answer back onto its own threads.

        Run between a fix round's re-delivery and the next review: the
        findings of every review round the fixer has since answered get a
        reply on their thread (resolved when addressed), and body-only
        findings one ``Reconciliation — round n`` comment. Best effort by
        design — the review that follows is worth more than a failed
        courtesy reply — but idempotent and resume-safe via the store.
        """
        run_id, ops, repo = p.run_id, p.ops, p.repo
        if ops is None or repo is None:
            return
        run = self.store.get_run(run_id)
        if run.pr_number is None:
            return
        posted = self.store.posted_findings(run_id)
        if not posted:
            return
        by_round: dict[int, list[PostedRecord]] = {}
        for rec in posted:
            by_round.setdefault(rec.round, []).append(rec)
        for round_ in self._review_rounds(run_id):
            # A round whose fix task has not reported yet has nothing to say.
            if not round_.response.strip():
                continue
            records = by_round.get(round_.round)
            if not records:
                continue
            items = reconcile(round_)
            if not items:
                continue
            try:
                outcome = reconcile_round(
                    ops,
                    repo,
                    run.pr_number,
                    run_id=run_id,
                    round=round_.round,
                    head_sha=run.head_sha,
                    posted=records,
                    items=items,
                    done=self.store.reconciliations(run_id, round_.round),
                    record=partial(self._record_reconciliation, run_id, round_.round),
                )
            except GithubOpsError:
                log.warning("review.reconcile_failed", run=run_id, pr=run.pr_number, exc_info=True)
                continue
            if outcome.did_anything:
                self._emit_reconciled(run_id, run.pr_number, outcome)
            self._reconcile_late_answers(p, run, round_, records, items)

    def _reconcile_late_answers(
        self,
        p: Pipeline,
        run: RunRecord,
        round_: ReviewRound,
        records: Sequence[PostedRecord],
        items: Mapping[str, Reconciliation],
    ) -> None:
        """A finding round *k* left unanswered rides into later briefs (#522);
        when a later round's report finally answers it, that answer belongs
        on the round-*k* thread. Replied under the later round's marker and
        record, so it is posted once and never mistaken for round *k*'s own
        "not answered" reply."""
        ops, repo, run_id = p.ops, p.repo, p.run_id
        assert ops is not None and repo is not None and run.pr_number is not None
        open_anchors = {a for a, item in items.items() if item.status == "unanswered"}
        if not open_anchors:
            return
        for later in self._review_rounds(run_id):
            if later.round <= round_.round or not later.response.strip() or not open_anchors:
                continue
            late = {a: reconcile_anchor(later.response, a) for a in sorted(open_anchors)}
            late = {a: item for a, item in late.items() if item.status != "unanswered"}
            if not late:
                continue
            try:
                outcome = reconcile_round(
                    ops,
                    repo,
                    run.pr_number,
                    run_id=run_id,
                    round=later.round,
                    head_sha=run.head_sha,
                    posted=[r for r in records if r.anchor in late],
                    items=late,
                    done=self.store.reconciliations(run_id, later.round),
                    record=partial(self._record_reconciliation, run_id, later.round),
                )
            except GithubOpsError:
                log.warning("review.reconcile_failed", run=run_id, pr=run.pr_number, exc_info=True)
                continue
            open_anchors -= set(late)
            if outcome.did_anything:
                self._emit_reconciled(run_id, run.pr_number, outcome)

    def _emit_reconciled(self, run_id: str, pr: int | None, outcome: ReconcileOutcome) -> None:
        self.bus.emit(
            HostEventTypes.REVIEW_RECONCILED,
            run_id,
            pr=pr,
            round=outcome.round,
            addressed=outcome.addressed,
            refuted=outcome.refuted,
            deferred=outcome.deferred,
            unanswered=outcome.unanswered,
            replied=outcome.replied,
            resolved=outcome.resolved,
            body_only=outcome.body_only,
            comment_url=outcome.comment_url or "",
        )

    def _stage_reconcile_human(self, p: Pipeline) -> None:
        """Answer the human objections the last fix round was seeded with.

        Their thread gets the reply; it is never resolved — a human closes
        their own conversation. Each answered objection is recorded so the
        next landing pass, which still sees the same standing
        ``CHANGES_REQUESTED`` (only its author can dismiss it), does not buy
        another full fix pass on words already answered (#520).
        """
        run_id, ops, repo = p.run_id, p.ops, p.repo
        objections = p.pending_human
        if ops is None or repo is None or not objections:
            return
        run = self.store.get_run(run_id)
        if run.pr_number is None:
            return
        report, round_no = self._last_fix_report(run_id)
        try:
            outcome = reconcile_human(
                ops,
                repo,
                run.pr_number,
                run_id=run_id,
                round=round_no,
                head_sha=run.head_sha,
                objections=objections,
                report=report,
                done=self.store.answered_objections(run_id),
                record=partial(self._record_human_reply, run_id),
            )
        except GithubOpsError:
            log.warning(
                "review.human_reconcile_failed", run=run_id, pr=run.pr_number, exc_info=True
            )
            return
        # Answered is answered even when the reply itself failed to post for
        # the body-only ones: what must not repeat is the fix pass.
        p.pending_human = ()
        if not outcome.did_anything:
            return
        self.bus.emit(
            HostEventTypes.REVIEW_RECONCILED,
            run_id,
            pr=run.pr_number,
            round=outcome.round,
            addressed=0,
            refuted=0,
            unanswered=0,
            replied=outcome.replied,
            resolved=0,
            body_only=outcome.body_only,
            comment_url=outcome.comment_url or "",
            human=len(objections),
        )

    def _record_human_reply(self, run_id: str, *, key: str, status: str) -> None:
        self.store.record_human_reply(run_id, key, status)

    def _last_fix_report(self, run_id: str) -> tuple[str, int]:
        """The most recent fix task's build report and its round number."""
        report, seen = "", []
        for row in self.store.phase_attempts(run_id):
            if row["phase"] != "build" or not row["task_id"]:
                continue
            task_id = str(row["task_id"])
            if not is_fix_task(task_id):
                continue
            if task_id not in seen:
                seen.append(task_id)
            try:
                report = str(json.loads(row["output_json"] or "{}").get("report") or "")
            except ValueError:
                report = ""
        return report, len(seen)

    def _record_reconciliation(
        self, run_id: str, round: int, *, anchor: str, status: str, resolved: bool
    ) -> None:
        self.store.record_reconciliation(run_id, round, anchor, status, resolved=resolved)

    def _diff_for_review(self, p: Pipeline, head_sha: str | None) -> str | None:
        """The PR's diff as text, or None when the workspace has no base
        to diff against (the reviewer then reads the tree)."""
        workspace = p.pair.workspace
        if workspace is None or not p.pair.mounted:
            return None
        # Diff against the *current* base commit, not the one the clone was
        # cut from: after a conflict round merged the base in, the latter
        # would show the whole base branch's movement as the run's changes.
        base_sha: str | None = None
        if p.ops is not None and p.repo is not None:
            try:
                base_sha = p.ops.ref_lookup(p.repo, f"heads/{self._base_branch(p)}")
            except SbxloopError:
                log.warning("review.base_lookup_failed", run=p.run_id, exc_info=True)
        try:
            return hostgit.diff_text(workspace, base_sha)
        except SbxloopError:
            log.warning("review.diff_failed", run=p.run_id, exc_info=True)
            return None

    def _review_rounds(self, run_id: str) -> list[ReviewRound]:
        """Earlier review rounds paired with the fix round each led to.

        Read from ``phase_attempts`` in order: a ``review`` row opens a
        round; the build report of the fix task recorded after it is that
        round's response. Chronology, not bookkeeping, so a resume sees the
        same history a live run would.
        """
        rounds: list[ReviewRound] = []
        for row in self.store.phase_attempts(run_id):
            if row["phase"] == "review":
                try:
                    data = json.loads(row["output_json"] or "{}")
                    verdict = ReviewVerdict.model_validate(data.get("verdict") or data)
                except (ValueError, ValidationError):
                    continue
                rounds.append(ReviewRound(len(rounds) + 1, verdict, ""))
            elif (
                row["phase"] == "build"
                and rounds
                and row["task_id"]
                and is_fix_task(str(row["task_id"]))
            ):
                try:
                    report = json.loads(row["output_json"] or "{}").get("report") or ""
                except ValueError:
                    report = ""
                last = rounds[-1]
                rounds[-1] = ReviewRound(last.round, last.verdict, str(report))
        return rounds

    def _review_posted(self, run_id: str) -> bool:
        """Whether the most recent review round got its record onto GitHub.

        Read from the ``review`` phase rows: a round that posted carries a
        url, a round whose post failed carries an empty one (#503). A run
        with no review round at all has nothing to have failed, so True.
        """
        for row in reversed(self.store.phase_attempts(run_id)):
            if row["phase"] != "review":
                continue
            try:
                data = json.loads(row["output_json"] or "{}")
            except ValueError:
                return False
            review = data.get("review") if isinstance(data, dict) else None
            return bool(isinstance(review, dict) and review.get("url"))
        return True

    def _repost_review_record(self, p: Pipeline, number: int) -> bool:
        """Self-heal a review round whose record never reached GitHub (#503).

        The review-posted gate exists so a merge never lacks a reviewable
        record — a requirement the loop can satisfy itself, so stranding a
        run on a transient 422/5xx would be a gate the loop erected. The
        repost is a PR **comment**, not a review: re-submitting the review
        would 422 again in the common self-review deployment (#513), and
        the findings/threads were already posted or reconciled. Idempotent
        by a run-scoped marker read back from the PR's comments, so a
        resume never double-posts. False — the terminal ``Blocked`` — only
        when the repost itself failed too: GitHub writes are then broadly
        failing, and blocking with the truth is honest.
        """
        run_id, ops, repo = p.run_id, p.ops, p.repo
        assert ops is not None and repo is not None
        stamp = f"<!-- sbxloop:review-record run={run_id} -->"
        try:
            existing = ops.raw("GET", f"/repos/{repo}/issues/{number}/comments")
        except GithubOpsError:
            existing = []
        for entry in existing if isinstance(existing, list) else []:
            if isinstance(entry, dict) and stamp in str(entry.get("body") or ""):
                return True
        rounds = self._review_rounds(run_id)
        if not rounds:
            return False
        last = rounds[-1]
        body = (
            f"**Review verdict: {last.verdict.verdict}** (round {last.round}) — "
            "reposted record: the review itself could not be posted (#503).\n\n"
            f"{last.verdict.summary}\n\n{stamp}"
        )
        try:
            ops.pr_issue_comment(repo, number, body)
        except GithubOpsError:
            log.warning("review.repost_failed", run=run_id, pr=number, exc_info=True)
            return False
        log.info("review.record_reposted", run=run_id, pr=number, round=last.round)
        return True

    def _reconcile_fix(
        self, run_id: str, task: TaskRecord, report: str
    ) -> list[dict[str, str]] | None:
        """The fix round's per-finding answer to the review that seeded it,
        as JSON for the build row. None for a non-fix task, or when no
        review round is open to reconcile against."""
        if not is_fix_task(task.spec.id):
            return None
        rounds = self._review_rounds(run_id)
        # The open round is the last one whose response is not yet recorded
        # — this build *is* that response.
        open_round = next((r for r in reversed(rounds) if not r.response.strip()), None)
        if open_round is None:
            return None
        # The brief also carried the findings earlier rounds left unanswered
        # (#522); this report is their answer too, or leaves them unanswered
        # again — either way they are judged here, not forgotten.
        carried = unanswered_findings([r for r in rounds if r is not open_round])
        seen = {f.anchor for f in open_round.verdict.findings}
        findings = [*open_round.verdict.findings, *[f for f in carried if f.anchor not in seen]]
        verdict = open_round.verdict.model_copy(update={"findings": findings})
        items = reconcile(ReviewRound(open_round.round, verdict, report))
        return [
            {"anchor": anchor, "status": item.status, "note": item.note, "test": item.test}
            for anchor, item in items.items()
        ]

    def _fix_round(
        self,
        p: Pipeline,
        kind: FixKind,
        why: str,
        *,
        findings: Sequence[ReviewFinding] = (),
        failed_checks: Sequence[FailedCheck] = (),
        objections: str = "",
        human: Sequence[HumanObjection] = (),
    ) -> str | None:
        """Spend one fix round: a seeded task built and verified like any
        other, then back to the gate. Returns the reason the run failed —
        the budget, or the task — or None when the fix is in."""
        run_id, phases = p.run_id, p.phases
        # Held for the reconciliation that follows the re-delivery: the
        # human hears back on their own thread, not only in a build report.
        p.pending_human = tuple(human)
        counter, configured = (
            ("review_rounds", self.config.landing.max_review_rounds)
            if kind == "review"
            else ("ci_rounds", self.config.landing.max_ci_rounds)
        )
        run = self.store.get_run(run_id)
        # Rounds granted after an exhaustion (by the daemon's retry or an
        # operator) extend the configured budget for this run only. The
        # counter is rounds actually spent, so it is checked before it is
        # bumped: a grant of N is then N real further rounds.
        limit = configured + run.granted_rounds
        already = run.review_rounds if counter == "review_rounds" else run.ci_rounds
        if already >= limit:
            budget_kind = "review" if kind == "review" else "ci"
            self.store.set_run_exhausted(run_id, budget_kind)
            granted = f" + {run.granted_rounds} granted" if run.granted_rounds else ""
            return (
                f"{kind} fix rounds exhausted ({configured} allowed by [landing] "
                f"{counter}{granted}): {why}"
            )
        spent = self.store.bump_run_counter(run_id, counter)
        tasks = self.store.get_tasks(run_id)
        round_no = 1 + sum(1 for t in tasks if is_fix_task(t.spec.id))
        verify_commands = [
            c for t in tasks if not is_fix_task(t.spec.id) for c in t.spec.verify_commands
        ]
        gate = phases.project_gate()
        if gate:
            verify_commands.append(gate)
        # Every fix round starts from the current base. CI judges GitHub's
        # test merge of the branch with the base, so a red check may only
        # exist in that merge (field run r8tzse1qa: a test that landed on
        # main after the run branched); a fixer working on a stale clone
        # cannot even reproduce it. A base that no longer merges cleanly
        # is left as conflict markers the brief lists.
        conflicts: tuple[str, ...] = ()
        merged = self._merge_base_into_clone(p)
        if merged is not None:
            conflicts = merged.conflicts
            if kind == "conflict" or conflicts:
                why = f"{why}; {merged.message}"
        rounds_so_far = self._review_rounds(run_id)
        spec = fix_task(
            round=round_no,
            pr_number=run.pr_number,
            brief=fix_brief(
                pr_number=run.pr_number,
                kind=kind,
                why=why,
                round=round_no,
                findings=findings,
                failed_checks=failed_checks,
                objections=objections,
                conflicts=conflicts,
                # The fixer is a fresh session: hand it what its
                # predecessors decided and why (#521) — and what they left
                # unanswered, which comes back until someone answers (#522).
                history=render_fix_history(rounds_so_far),
                unanswered=unanswered_findings(rounds_so_far),
            ),
            verify_commands=verify_commands,
            failed_checks=failed_checks,
        )
        task = self.store.append_task(run_id, spec)
        p.fix_kinds[spec.id] = kind
        self.bus.emit(
            HostEventTypes.FIX_ROUND,
            run_id,
            round=round_no,
            kind=kind,
            task_id=spec.id,
            why=why,
            budget=f"{spent}/{limit}",
            pr=run.pr_number,
        )
        self._announce_roster(run_id, self.store.get_tasks(run_id))
        return self._drive_fix_task(p, task)

    def _base_branch(self, p: Pipeline) -> str:
        """The branch the PR targets: configured, else the repository's default."""
        base = p.repo_config.deliver_base if p.repo_config else self.config.github.deliver_base
        if base:
            return base
        if p.ops is not None and p.repo is not None:
            return str(p.ops.repo_get(p.repo).get("default_branch") or "main")
        return "main"

    def _merge_base_into_clone(self, p: Pipeline) -> hostgit.MergeResult | None:
        """Before a fix round: bring the current base into the run's clone,
        so the fixer works on what CI actually judges and a conflict is
        real in its working tree (see :func:`hostgit.merge_from_base`).
        None when the run has no mounted clone to merge into; a fetch/merge
        failure is logged and the round proceeds on the tree as it is."""
        workspace = p.pair.workspace
        if workspace is None or not p.pair.mounted or p.ops is None:
            return None
        base = self._base_branch(p)
        try:
            result = hostgit.merge_from_base(workspace, base)
        except SbxloopError:
            log.warning("fix.merge_base_failed", run=p.run_id, base=base, exc_info=True)
            return None
        log.info(
            "fix.merged_base",
            run=p.run_id,
            base=base,
            merged=result.merged,
            conflicts=list(result.conflicts),
        )
        return result

    def _resume_fix(self, p: Pipeline) -> str | None:
        """A run resumed mid fix round: finish the fix task still in flight
        (none means the task ended before the stage moved on — nothing to do)."""
        pending = [
            t for t in self.store.get_tasks(p.run_id) if is_fix_task(t.spec.id) and not t.terminal
        ]
        if not pending:
            return None
        self._announce_roster(p.run_id, self.store.get_tasks(p.run_id))
        return self._drive_fix_task(p, pending[-1])

    def _drive_fix_task(self, p: Pipeline, task: TaskRecord) -> str | None:
        self._set_run_state(p.run_id, "fixing")
        self._run_task(p, task)
        if task.state != "done":
            return f"fix round {task.spec.id} failed: {task.last_feedback[:300] or task.state}"
        return None

    def _stage_ci(self, p: Pipeline) -> NeedsFix | Blocked | None:
        """Wait for CI on the delivered head; None means green."""
        run_id, ops, repo = p.run_id, p.ops, p.repo
        assert ops is not None and repo is not None
        self._set_run_state(run_id, "awaiting_ci")
        run = self.store.get_run(run_id)
        if not run.head_sha:
            return Blocked("no delivered head to check")
        round_no = 1 + sum(1 for t in self.store.get_tasks(run_id) if is_fix_task(t.spec.id))

        def emit(**data: Any) -> None:
            self.bus.emit(
                HostEventTypes.CI_STATUS, run_id, pr=run.pr_number, round=round_no, **data
            )

        try:
            verdict = poll_checks(
                ops,
                repo,
                run.head_sha,
                cfg=self.config.landing,
                tick=partial(self._tick, p),
                emit=emit,
                clock=self.clock,
                settle_from=p.delivered_at,
            )
        except CiTimeout as exc:
            return Blocked(str(exc))
        if verdict.state == "red":
            return NeedsFix(
                "ci",
                verdict.summary(),
                failed_checks=tuple(ops.checks_failed_logs(repo, run.head_sha)),
            )
        return None

    def _stage_land(self, p: Pipeline) -> Landed | Gated | Blocked | NeedsFix | Closed:
        run_id, ops, repo = p.run_id, p.ops, p.repo
        assert ops is not None and repo is not None
        self._set_run_state(run_id, "landing")
        run = self.store.get_run(run_id)
        assert run.pr_number is not None
        number = run.pr_number
        update = UpdateState(attempts=run.update_attempts, head=run.update_head)

        def on_update(state: UpdateState) -> None:
            self.store.bump_run_counter(run_id, "update_attempts")
            self.store.set_update_head(run_id, state.head)

        def emit(type: str, **data: Any) -> None:
            self.bus.emit(type, run_id, **data)

        login = self._login(p)
        outcome = land(
            ops,
            repo,
            number,
            cfg=self.config.landing,
            branch=run.branch,
            node_id=run.pr_node_id,
            login=login,
            update=update,
            on_update=on_update,
            tick=partial(self._tick, p),
            emit=emit,
            clock=self.clock,
            answered=self.store.answered_objections(run_id),
            review_posted=self._review_posted(run_id) or self._repost_review_record(p, number),
            ack=lambda threads: acknowledge_human_threads(
                ops, repo, number, run_id=run_id, login=login, threads=threads
            ),
            gate=self.config.landing.merge_gate == "chat",
        )
        if isinstance(outcome, Landed):
            log.info(
                "run.merged", run=run_id, pr=number, sha=outcome.sha, by_human=outcome.by_human
            )
            self.bus.emit(
                HostEventTypes.RUN_MERGED,
                run_id,
                pr=number,
                url=run.pr_url,
                sha=outcome.sha,
                by_human=outcome.by_human,
                review_rounds=run.review_rounds,
                ci_rounds=run.ci_rounds,
            )
            # Only now (#517): a run that failed or was blocked files nothing.
            self._file_followups(p, run)
        elif isinstance(outcome, Gated):
            log.info("run.gated", run=run_id, pr=number, head=outcome.head)
            self.bus.emit(
                HostEventTypes.RUN_GATED,
                run_id,
                pr=number,
                url=run.pr_url,
                sha=outcome.head,
                review_rounds=run.review_rounds,
                ci_rounds=run.ci_rounds,
            )
            # The approve path is gh-ops-only — no engine, no sandbox — so
            # the follow-ups are filed now, while the machinery that builds
            # them is alive. Gate is not blocked: every bar was cleared, and
            # follow-ups carry the follow-up label, never the trigger label.
            self._file_followups(p, run)
        elif isinstance(outcome, Blocked):
            log.warning("run.blocked", run=run_id, pr=number, why=outcome.why)
            self.bus.emit(
                HostEventTypes.RUN_BLOCKED, run_id, pr=number, url=run.pr_url, why=outcome.why
            )
        return outcome

    def _file_followups(self, p: Pipeline, run: RunRecord) -> None:
        """File the run's follow-ups on its repository after the merge (#517).

        Best-effort and idempotent: the PR is merged, so a GitHub failure
        here is logged, never raised. Each filed issue is recorded as a
        ``followup`` phase row before the next is filed, and the body carries
        a run/key marker, so a resume between filing and recording finds the
        issue on the repository rather than filing it twice. Never queued for
        the loop: the follow-up label, not the trigger label.
        """
        ops, repo, run_id = p.ops, p.repo, p.run_id
        cfg = self.config.landing
        if ops is None or repo is None or cfg.followups == "off" or run.pr_number is None:
            return
        candidates = collect_followups(self._review_rounds(run_id))[: cfg.max_followups_per_run]
        if not candidates:
            return
        already = self._recorded_followups(run_id)
        filed: list[tuple[str, str]] = []
        listed: list[str] = []
        started = time.time()
        try:
            if cfg.followups == "issues":
                on_repo = self._filed_on_repo(ops, repo, cfg.followup_label, run_id)
                self._ensure_label(ops, repo, cfg.followup_label)
                for cand in candidates:
                    title = cand.followup.title.strip()
                    if cand.key in already:
                        filed.append((title, already[cand.key]))
                        continue
                    if cand.key in on_repo:
                        url = on_repo[cand.key]
                    else:
                        ref = ops.issue_create(
                            repo,
                            title,
                            issue_body(
                                cand,
                                run_id=run_id,
                                repo=repo,
                                pr_number=run.pr_number,
                                pr_url=run.pr_url or "",
                                closes=self.config.github.deliver_closes,
                            ),
                            labels=[cfg.followup_label],
                        )
                        url = ref.url
                    filed.append((title, url))
                    self.store.record_phase(
                        run_id,
                        "followup",
                        task_id=None,
                        attempt=len(already) + len(filed),
                        status="filed",
                        output_json=json.dumps({"key": cand.key, "title": title, "url": url}),
                        started_at=started,
                    )
                    already[cand.key] = url
                if filed and "(comment)" not in already:
                    # One pointer on the PR, so the human sees them without
                    # opening the tracker.
                    ops.pr_issue_comment(
                        repo,
                        run.pr_number,
                        checklist_comment(candidates, run_id=run_id, filed=filed),
                    )
                    self._record_followup_comment(run_id, len(already) + 1, len(filed), started)
            else:
                if "(comment)" not in already:
                    ops.pr_issue_comment(
                        repo, run.pr_number, checklist_comment(candidates, run_id=run_id)
                    )
                    self._record_followup_comment(
                        run_id, len(already) + 1, len(candidates), started
                    )
                listed = [c.followup.title.strip() for c in candidates]
        except GithubOpsError:
            log.warning("run.followups_failed", run=run_id, pr=run.pr_number, exc_info=True)
        if not filed and not listed:
            return
        log.info(
            "run.followups",
            run=run_id,
            pr=run.pr_number,
            mode=cfg.followups,
            filed=[url for _, url in filed],
            listed=len(listed),
        )
        self.bus.emit(
            HostEventTypes.RUN_FOLLOWUPS,
            run_id,
            pr=run.pr_number,
            mode=cfg.followups,
            filed=[{"title": t, "url": u} for t, u in filed],
            listed=listed,
        )

    def _record_followup_comment(
        self, run_id: str, attempt: int, count: int, started: float
    ) -> None:
        self.store.record_phase(
            run_id,
            "followup",
            task_id=None,
            attempt=attempt,
            status="listed",
            output_json=json.dumps({"key": "(comment)", "count": count}),
            started_at=started,
        )

    def _recorded_followups(self, run_id: str) -> dict[str, str]:
        """``{key: url}`` of the follow-ups this run already filed (or
        ``"(comment)"`` when the checklist comment was posted)."""
        out: dict[str, str] = {}
        for row in self.store.phase_attempts(run_id):
            if row["phase"] != "followup":
                continue
            try:
                data = json.loads(row["output_json"] or "{}")
            except ValueError:
                continue
            key = str(data.get("key") or "")
            if key:
                out[key] = str(data.get("url") or "")
        return out

    @staticmethod
    def _filed_on_repo(ops: GithubOps, repo: str, label: str, run_id: str) -> dict[str, str]:
        """Follow-ups already on the repository for this run, by key — the
        crash-window dedup (filed, died before recording). Read from the
        label's issue list, which unlike search is not eventually consistent."""
        out: dict[str, str] = {}
        try:
            data = ops.raw(
                "GET",
                f"/repos/{repo}/issues?labels={quote(label, safe='')}&state=all&per_page=100",
            )
        except GithubOpsError:
            log.warning("run.followups_list_failed", repo=repo, exc_info=True)
            return out
        for issue in data if isinstance(data, list) else []:
            if not isinstance(issue, dict):
                continue
            found = marker_key(str(issue.get("body") or ""))
            if found and found[0] == run_id:
                out[found[1]] = str(issue.get("html_url") or "")
        return out

    @staticmethod
    def _ensure_label(ops: GithubOps, repo: str, label: str) -> None:
        """Make sure the repository carries the follow-up label.

        A label that already exists is an expected condition, not an error:
        we look it up first and return silently when it is there, so the run
        never records a failed creation call. The lookup goes through
        ``label_lookup``, which answers a 404 as data rather than as a failed
        worker job — the same treatment ``ref_lookup`` gives an absent branch
        (#518), so a repository *without* the label does not pay a red panel
        for asking. Only a genuinely missing label is POSTed, and the 422
        catch still covers the race between the two calls. A refusal must not
        stop the filing — GitHub accepts an issue whose label it cannot find.

        This is the only place that creates a *repository* label; the other
        ``/labels`` calls (engine issue filing, daemon sources/concierge)
        attach or remove labels on an issue, where an existing label is
        already the happy path."""
        try:
            existing = ops.label_lookup(repo, label)
        except GithubOpsError as exc:
            # Not a 404 — no repo scope, or GitHub is unwell. One warning,
            # and no doomed POST behind it.
            log.warning("run.followup_label_failed", repo=repo, label=label, error=str(exc))
            return
        if existing:
            log.debug("run.followup_label_present", repo=repo, label=label)
            return
        try:
            ops.raw(
                "POST",
                f"/repos/{repo}/labels",
                {"name": label, "color": "c5def5", "description": "filed by sbxloop after a merge"},
            )
        except GithubOpsError as exc:
            text = str(exc)
            exists = "already_exists" in text or "already exists" in text
            if exc.http_status == 422 or exists:
                log.debug("run.followup_label_present", repo=repo, label=label)
                return
            log.warning("run.followup_label_failed", repo=repo, label=label, error=text)

    def _login(self, p: Pipeline) -> str:
        """The loop's own GitHub login, read once per drive.

        Resolution order: the App's own ``<slug>[bot]`` when the host
        resolved one (``Pipeline.bot_login`` — App mode skips ``GET
        /user``, which an installation token cannot call: 403, #581); then
        ``GET /user`` (PAT mode); then the author of the delivered PR (the
        same token opened it); then ``""`` with a plain log line. The empty
        degradation is **not** harmless for landing — classification would
        call every loop thread a human's — so landing refuses to classify
        with it (`_reconciliation_block`) instead of misclassifying.
        """
        if p.login is None:
            assert p.ops is not None and p.repo is not None
            number = self.store.get_run(p.run_id).pr_number
            p.login = resolve_login(p.ops, p.repo, number, bot_login=p.bot_login)
            log.info("engine.login_resolved", run=p.run_id, login=p.login or "(unknown)")
        return p.login

    def _tick(self, p: Pipeline, waiting: str) -> None:
        """One wait interval between GitHub polls: honour cancellation,
        answer chat, keep the run visibly alive, then sleep — cut short by
        anything that sets ``_wake``. The slept time is not charged to the
        agent wall clock."""
        run_id = p.run_id
        self._check_cancelled_and_clock(run_id, p.deadline)
        run = self.store.get_run(run_id)
        self._process_chat(
            run_id,
            p.phases,
            None,
            stage=f"{run.state} on PR #{run.pr_number} (waiting on {waiting})",
        )
        self.store.touch_run(run_id)
        started = self.clock()
        self._wake.wait(self.config.landing.ci_poll_interval_s)
        self._wake.clear()
        self._waited_s += max(0.0, self.clock() - started)
        self._check_cancelled_and_clock(run_id, p.deadline)
        # A message is what usually cut the wait short; answer it now rather
        # than after a poll that may end the run.
        self._process_chat(
            run_id,
            p.phases,
            None,
            stage=f"{run.state} on PR #{run.pr_number} (waiting on {waiting})",
        )

    # -- task state machine ------------------------------------------------

    def _run_task(self, p: Pipeline, task: TaskRecord) -> None:
        run_id, phases, pair, granter, deadline = p.run_id, p.phases, p.pair, p.granter, p.deadline
        if task.state == "pending":
            self._set_task_state(run_id, task, "executing")
        self.bus.emit(
            HostEventTypes.TASK_START, run_id, task_id=task.spec.id, title=task.spec.title
        )

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
            if task.state == "executing":
                self._phase_build(run_id, phases, task, granter)
            elif task.state == "verifying":
                self._phase_verify(run_id, phases, task)
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

    def _phase_build(
        self,
        run_id: str,
        phases: PhaseRunner,
        task: TaskRecord,
        granter: EgressGranter,
    ) -> None:
        # Grant-late: task-declared egress is applied at BUILD entry, not at
        # decompose time, so resumed tasks get their grants on the freshly
        # provisioned sandbox. The grant rewrites the shared sandbox's
        # network policy, so lanes take it in turn rather than interleaving
        # inside it.
        with self._sandbox_lock:
            granter.apply(
                task.spec.id, [(egress.domain, egress.reason) for egress in task.spec.egress]
            )
        started = time.time()
        # A revision continues the same agent's own work on the same task, so
        # it resumes that session where the SDK still has it and is handed the
        # previous attempt's report either way. Both answer the same waste:
        # without them a revision re-establishes everything the last attempt
        # already knew. Resume is the stronger of the two but the more
        # fragile — it needs the session to still exist — so the report is
        # passed unconditionally rather than only as a fallback.
        resume = task.session_id if task.session_id in self._live_sessions else None
        result = phases.build(
            task,
            prior_report=self._prior_attempt_report(run_id, task),
            resume_session_id=resume,
        )
        if resume and result.session_id != resume:
            # The backend falls back to a fresh session when a resume fails
            # rather than failing the job. It cannot say so (no logger in
            # the worker), but a different id coming back is the tell — and
            # without this line a silently-never-resuming pipeline would
            # look identical to a working one.
            log.info(
                "phase.resume_missed",
                run=run_id,
                task=task.spec.id,
                requested=resume,
                got=result.session_id,
                hint="the SDK could not resume; the prior report still carried the context",
            )
        task.session_id = result.session_id
        if result.session_id:
            self._live_sessions.add(result.session_id)
        builder_report = clip(result.output_text)
        spend = phases.drain_spend()
        payload: dict[str, Any] = {"report": builder_report, "session_id": result.session_id}
        # A fix round's report is the per-finding answer to the review that
        # seeded it. Parse it once, here, and persist it with the build row:
        # the reconciliation that gets replied onto the PR threads must
        # survive a resume, and re-deriving it later depends on the report
        # still being parseable by whatever the code says then.
        reconciled = self._reconcile_fix(run_id, task, builder_report)
        if reconciled is not None:
            payload["reconciled"] = reconciled
            unanswered = [r["anchor"] for r in reconciled if r["status"] == "unanswered"]
            if unanswered:
                # Loud, not fatal (#522): a round that says nothing about a
                # finding is incomplete; the finding rides into the next brief.
                log.warning(
                    "fix.unanswered_findings",
                    run=run_id,
                    task=task.spec.id,
                    anchors=unanswered,
                    hint="the fix report has no addressed/refuted/deferred line for these; "
                    "they are carried into the next fix round as unanswered",
                )
                self.bus.emit(
                    HostEventTypes.FIX_UNANSWERED,
                    run_id,
                    round=sum(1 for t in self.store.get_tasks(run_id) if is_fix_task(t.spec.id)),
                    task_id=task.spec.id,
                    anchors=unanswered,
                )
        self.store.record_phase(
            run_id,
            "build",
            task_id=task.spec.id,
            attempt=task.revisions + 1,
            status="ok",
            output_json=json.dumps(payload),
            started_at=started,
            usage=spend.usage,
            turns=spend.turns,
        )
        # The report excerpt is the chronology's record of what this attempt
        # did (the builder narrates its approach in prose now that no
        # structured plan exists); the streamed agent messages carry the
        # detail.
        headline = " ".join((result.output_text or "").split())
        self.bus.emit(
            HostEventTypes.PHASE_END,
            run_id,
            task_id=task.spec.id,
            phase="build",
            status="ok",
            attempt=task.revisions + 1,
            message=headline[:300] or "(builder produced no report)",
        )
        task.last_feedback = ""
        self._set_task_state(run_id, task, "verifying")

    def _phase_verify(
        self,
        run_id: str,
        phases: PhaseRunner,
        task: TaskRecord,
    ) -> None:
        started = time.time()
        outcome = phases.verify(task)
        passed, feedback, results = outcome.passed, outcome.feedback, outcome.results
        # `results` (the full command transcript) is persisted so a resumed
        # run reads the evidence from phase_attempts rather than in-memory
        # state (#61).
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
            self._set_task_state(run_id, task, "done")
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
        repeated = self._record_verify_failures(task, outcome.failures)
        if repeated and task.verify_suspect:
            # Already flagged: keep the suspect wording (never fall back to
            # plain "revise the code" feedback for a check we know repeats)
            # but do not spend another replan on the same diagnosis.
            # `verify_failure` stays False deliberately: its exhaustion path
            # spends a replan AND overwrites last_feedback with the generic
            # "start over with a fresh approach" text, which would undo the
            # very wording this branch exists to preserve.
            self._register_revision(run_id, task, verify_suspect_feedback(repeated))
            return
        if repeated:
            # Mechanical verify-suspect signal (#387): the same command has
            # now failed with the same (normalised) output on a later
            # attempt, so another revision of the code cannot change the
            # result. Route straight to a replan that says the check is
            # what needs re-authoring — no model call is needed to see it.
            task.verify_suspect = True
            self.bus.emit(
                HostEventTypes.PHASE_END,
                run_id,
                task_id=task.spec.id,
                phase="verify",
                status="failed",
                message=(
                    f"verify command suspect: `{repeated[0].command}` failed identically again"
                ),
            )
            self._register_verify_suspect(run_id, task, repeated)
            return
        self._register_revision(run_id, task, feedback, verify_failure=True)

    @staticmethod
    def _record_verify_failures(
        task: TaskRecord, failures: Sequence[VerifyFailure]
    ) -> list[VerifyFailure]:
        """Fingerprint this attempt's verify failures against everything the
        task has seen before; return the ones that are exact repeats."""
        seen = set(task.verify_fingerprints)
        repeated: list[VerifyFailure] = []
        for failure in failures:
            fingerprint = failure.fingerprint
            if fingerprint in seen:
                repeated.append(failure)
            else:
                seen.add(fingerprint)
                task.verify_fingerprints.append(fingerprint)
        return repeated

    def _register_verify_suspect(
        self, run_id: str, task: TaskRecord, repeated: Sequence[VerifyFailure]
    ) -> None:
        """Spend one fresh-session attempt on the only thing the loop can do.

        The verify commands are decomposer-authored and the builder is told
        it cannot edit them, so this does NOT order a re-author (the review
        of #509 was right that nothing in the loop re-runs decompose). What
        it does is give the one remaining lever a fresh session: an approach
        whose layout and setup satisfy the command exactly as written. The
        feedback also tells the builder to report the command as unpassable
        if it cannot be satisfied, and ``verify_suspect`` is carried into the
        run's failure reason (``_failure_reason``) so the diagnosis reaches a
        human rather than dying in the task row.

        Deliberately does NOT increment ``revisions``: the whole point of
        the signal is that identical revisions are wasted (field run
        rrhb28j7n burned two revisions and a replan on one impossible
        ``uv run mypy packages``).
        """
        task.last_feedback = verify_suspect_feedback(list(repeated))
        if task.replans >= self.config.budgets.max_replans_per_task:
            self._set_task_state(run_id, task, "failed")
            return
        task.replans += 1
        self._discard_session(task)
        self._set_task_state(run_id, task, "executing")

    @staticmethod
    def _discard_session(task: TaskRecord) -> None:
        """Throw away the current approach so BUILD starts clean.

        A *revision* continues the same approach, so resuming that session
        is the whole point — it is what stops the next attempt re-deriving
        what this one established. A *replan* (or a steer) is the opposite:
        the approach itself was wrong, and a resumed session would carry the
        discarded one forward as though it were still the intent. Clearing
        the id here is what keeps those two cases apart.
        """
        task.revisions = 0
        task.session_id = None

    def _prior_attempt_report(self, run_id: str, task: TaskRecord) -> str:
        """What the previous BUILD attempt on this task said it did.

        Field failure (run rrhb28j7n, task t5): five executor sessions each
        ran ``uv sync --all-packages`` and the whole lint gate from scratch
        and each concluded "no changes needed", because a revision was told
        only what the critic objected to and nothing about what the last
        attempt had already established. The report is committed to
        phase_attempts either way — this is the engine handing back work it
        was already holding, so a revision starts from the last attempt's
        findings instead of re-deriving them.

        Read from the store rather than kept in memory so a resumed run's
        revision gets the same context a fresh one would.
        """
        output = self.store.latest_phase_output(run_id, task.spec.id, "build")
        if output is None:
            return ""
        report = json.loads(output).get("report")
        return report if isinstance(report, str) else ""

    def _register_revision(
        self, run_id: str, task: TaskRecord, feedback: str, *, verify_failure: bool = False
    ) -> None:
        task.revisions += 1
        task.last_feedback = feedback
        if task.revisions <= self.config.budgets.max_revisions_per_task:
            self._set_task_state(run_id, task, "executing")
            return
        if verify_failure and task.replans < self.config.budgets.max_replans_per_task:
            # Verify commands are decomposer-authored; the builder cannot
            # edit them, so no number of revisions inside one session can
            # fix an approach that disagrees with where the checks look. A
            # fresh session starts the approach over and can route the work
            # to where the commands expect files.
            task.replans += 1
            self._discard_session(task)
            task.last_feedback = (
                "every revision failed the same verify commands; start over "
                "with a fresh approach whose file layout and setup satisfy "
                "the commands exactly as written:\n\n" + feedback
            )
            self._set_task_state(run_id, task, "executing")
            return
        self._set_task_state(run_id, task, "failed")

    # -- interactive chat --------------------------------------------------

    def _process_chat(
        self,
        run_id: str,
        phases: PhaseRunner,
        task: TaskRecord | None,
        *,
        stage: str | None = None,
    ) -> None:
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
            self._drain_chat(run_id, phases, task, stage)
        finally:
            self._chat_lock.release()

    def _drain_chat(
        self, run_id: str, phases: PhaseRunner, task: TaskRecord | None, stage: str | None
    ) -> None:
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
                verdict = phases.steer(
                    message.text, tasks=self.store.get_tasks(run_id), task=task, stage=stage
                )
            except WorkerError as exc:
                log.warning(
                    "run.steer_failed",
                    run=run_id,
                    message=message.message_id,
                    attempt=self._steer_attempts,
                    exc_info=True,
                )
                spend = phases.drain_spend()
                self.store.record_phase(
                    run_id,
                    "steer",
                    task_id=task.spec.id if task else None,
                    attempt=self._steer_attempts,
                    status="error",
                    output_json=json.dumps({"message": message.text, "error": str(exc)}),
                    started_at=started,
                    usage=spend.usage,
                    turns=spend.turns,
                )
                self.bus.emit(
                    HostEventTypes.CHAT_REPLY,
                    run_id,
                    message_id=message.message_id,
                    error=str(exc),
                )
                continue
            action = self._apply_steer(run_id, task, verdict, phases)
            spend = phases.drain_spend()
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
                usage=spend.usage,
                turns=spend.turns,
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
            # User direction, not a failure: the task's build session is
            # discarded and restarted with the guidance as feedback, and
            # neither budget counter is spent.
            task.last_feedback = f"user steering (must be honored): {verdict.guidance}"
            self._discard_session(task)
            self._set_task_state(run_id, task, "executing")
            self.bus.emit(
                HostEventTypes.CHAT_ACTION,
                run_id,
                task_id=task.spec.id,
                action=action,
                guidance=verdict.guidance,
                message=(
                    f"user steering: restarting task {task.spec.id} with guidance — "
                    f"{verdict.guidance}"
                ),
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
        # Waiting on GitHub is not agent work; it is not charged to the budget.
        if self.clock() - self._waited_s > deadline:
            self._set_run_state(run_id, "failed")
            self.store.set_run_reason(
                run_id, f"exceeded max_wall_clock_s={self.config.budgets.max_wall_clock_s:g}"
            )
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
