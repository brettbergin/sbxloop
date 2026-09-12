"""Model policy is independent of permissions, task kind and provider recovery."""

from __future__ import annotations

import json
import tomllib
from contextlib import closing
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from sbxloop.agentmodels import model_for_phase, refreshed_models
from sbxloop.config import AgentModels, Config, load_config
from sbxloop.engine.model import TaskRecord, TaskSpec
from sbxloop.engine.phases import AGENT_NAMES, PhaseRunner
from sbxloop.engine.store import StateStore
from sbxloop.errors import ConfigError
from sbxloop_worker.protocol import JobRequest, JobResult


def config(**extra: Any) -> Config:
    return Config.model_validate(
        {
            "model": "fallback",
            "agent": {"models": {"build": "strong", "review": "medium"}},
            "concierge": {"model": "small"},
            "github": {
                "repos": [
                    {"repo": "org/one", "agent_models": {"review": "review-one"}},
                    {"repo": "org/two", "agent_models": {"build": "build-two"}},
                ]
            },
            **extra,
        }
    )


def test_all_agent_phases_have_independent_settings() -> None:
    assert set(AgentModels.model_fields) == set(AGENT_NAMES)
    settings = {phase: f"model-{phase}" for phase in AGENT_NAMES}
    cfg = config(agent={"models": settings})
    for phase in AGENT_NAMES:
        assert model_for_phase(cfg, phase).model == settings[phase]


def test_sparse_repository_inheritance_and_no_implicit_default_repo() -> None:
    cfg = config()
    assert model_for_phase(cfg, "review", repo="org/one").model == "review-one"
    assert model_for_phase(cfg, "build", repo="org/one").model == "strong"
    assert model_for_phase(cfg, "review", repo="org/two").model == "medium"
    assert model_for_phase(cfg, "build", repo="org/two").model == "build-two"
    assert model_for_phase(cfg, "decompose", repo="org/one").model == "fallback"
    assert model_for_phase(cfg, "review").model == "medium"
    assert model_for_phase(cfg, "concierge", repo="org/one").model == "small"
    narrowed = cfg.model_copy(update={"github": cfg.github.for_repo("org/one")})
    assert model_for_phase(narrowed, "review", repo="org/one").model == "review-one"


def test_cli_override_survives_snapshot_and_does_not_change_concierge() -> None:
    cfg = config(run_model_override="forced")
    restored = Config.model_validate_json(cfg.model_dump_json())
    for phase in AGENT_NAMES:
        assert model_for_phase(restored, phase, repo="org/one").model == "forced"
    assert model_for_phase(restored, "concierge").model == "small"


def test_explicit_auto_stops_inheritance_and_legacy_defaults_load() -> None:
    cfg = config(agent={"models": {"build": "auto"}})
    assert model_for_phase(cfg, "build").model == "auto"
    legacy = Config.model_validate_json('{"model":"legacy"}')
    assert all(model_for_phase(legacy, phase).model == "legacy" for phase in AGENT_NAMES)
    assert model_for_phase(Config(concierge={"model": ""}), "concierge").model == "auto"


@pytest.mark.parametrize("value", ["", "  ", 123, False, [], {}])
def test_invalid_model_values_fail_at_load(value: Any) -> None:
    with pytest.raises(ValidationError):
        config(agent={"models": {"build": value}})


def test_unknown_role_fails_in_configuration_and_dispatch() -> None:
    with pytest.raises(ValidationError):
        config(agent={"models": {"buidl": "strong"}})
    with pytest.raises(ValueError, match="phase"):
        model_for_phase(Config(), "buidl")


def test_live_reload_preserves_non_model_settings_and_env_precedence(tmp_path) -> None:
    path = tmp_path / "sbxloop.toml"
    path.write_text('model = "old"\n[budgets]\nmax_tasks = 2\n')
    cfg = load_config(tmp_path, env={"SBXLOOP_AGENT__MODELS__BUILD": "env-build"})
    path.write_text(
        'model = "new"\n[budgets]\nmax_tasks = 9\n[agent.models]\nreview = "new-review"\n'
    )
    fresh = refreshed_models(cfg)
    assert fresh.budgets.max_tasks == 2
    assert model_for_phase(fresh, "build").model == "env-build"
    assert model_for_phase(fresh, "review").model == "new-review"
    assert model_for_phase(fresh, "steer").model == "new"
    assert cfg.model == "old"


def test_live_reload_rejects_backend_changes_and_broken_config(tmp_path) -> None:
    path = tmp_path / "sbxloop.toml"
    path.write_text('model = "old"\n')
    cfg = load_config(tmp_path, env={})
    path.write_text('[agent]\nbackend = "claude"\n')
    with pytest.raises(ConfigError, match="backend"):
        refreshed_models(cfg)
    path.write_text('[agent.models]\nbuild = ""\n')
    with pytest.raises(ConfigError):
        refreshed_models(cfg)


class RecordingAgent:
    def __init__(self) -> None:
        self.jobs: list[JobRequest] = []
        self.after_submit = lambda: None
        self.outputs: list[Any] = []

    def submit(self, job: JobRequest, **kwargs: Any) -> JobResult:
        self.jobs.append(job)
        self.after_submit()
        return JobResult(
            job_id=job.job_id,
            status="ok",
            session_id=f"session-{len(self.jobs)}",
            output_text="The previous report",
            output_json=self.outputs.pop(0) if self.outputs else None,
        )


@pytest.mark.parametrize("phase", list(AGENT_NAMES))
def test_each_phase_submits_its_selected_model(phase: str) -> None:
    agent = RecordingAgent()
    cfg = config(agent={"models": {phase: "selected"}}, github={})
    runner = PhaseRunner(agent, cfg, "r1", "task")  # type: ignore[arg-type]
    runner._agent_job("brief", phase=phase, permission_mode="read_only", expect="text")
    assert agent.jobs[0].model == "selected"
    assert agent.jobs[0].permission_mode == "read_only"


def test_json_repair_keeps_model_but_next_phase_rereads(tmp_path) -> None:
    path = tmp_path / "sbxloop.toml"
    path.write_text('[agent.models]\nsteer = "first"\n')
    agent = RecordingAgent()
    agent.outputs = [{"invalid": True}, {"reply": "ok", "action": "continue"}] * 2
    agent.after_submit = lambda: path.write_text('[agent.models]\nsteer = "next"\n')
    runner = PhaseRunner(agent, load_config(tmp_path, env={}), "r1", "task")  # type: ignore[arg-type]
    runner.steer("hello", tasks=[], task=None)
    runner.steer("again", tasks=[], task=None)
    assert [job.model for job in agent.jobs] == ["first", "first", "next", "next"]


def test_changed_model_discards_session_but_preserves_request_context(tmp_path) -> None:
    path = tmp_path / "sbxloop.toml"
    path.write_text('[agent.models]\nbuild = "first"\n')
    agent = RecordingAgent()
    runner = PhaseRunner(agent, load_config(tmp_path, env={}), "r1", "task")  # type: ignore[arg-type]
    first = runner._agent_job("brief", phase="build", permission_mode="auto", expect="text")
    runner._agent_job(
        "prior report: completed step",
        phase="build",
        permission_mode="auto",
        expect="text",
        resume_session_id=first.session_id,
    )
    assert agent.jobs[-1].resume_session_id == first.session_id
    path.write_text('[agent.models]\nbuild = "second"\n')
    runner._agent_job(
        "prior report: completed step",
        phase="build",
        permission_mode="auto",
        expect="text",
        resume_session_id=first.session_id,
    )
    assert agent.jobs[-1].model == "second"
    assert agent.jobs[-1].resume_session_id is None
    assert "completed step" in agent.jobs[-1].prompt


@pytest.mark.parametrize(("phase", "method"), [("build", "build"), ("operator_execute", "execute")])
def test_revision_after_model_change_keeps_workspace_and_reports(tmp_path, phase, method):
    path = tmp_path / "sbxloop.toml"
    path.write_text(f'[agent.models]\n{phase} = "first"\n')
    agent = RecordingAgent()
    runner = PhaseRunner(agent, load_config(tmp_path, env={}), "r1", "task", workdir="/work")
    task = TaskRecord(
        spec=TaskSpec(id="t1", title="Finish the output"), last_feedback="Fix the title"
    )
    first = getattr(runner, method)(task)
    path.write_text(f'[agent.models]\n{phase} = "second"\n')
    getattr(runner, method)(
        task, prior_report="Completed step one", resume_session_id=first.session_id
    )
    sent = agent.jobs[-1]
    assert sent.resume_session_id is None and sent.model == "second"
    assert sent.cwd == "/work" and sent.permission_mode == "auto"
    assert "Completed step one" in sent.prompt and "Fix the title" in sent.prompt
    assert "Finish the output" in sent.prompt


def test_repository_model_edits_stay_isolated_and_sparse(tmp_path):
    path = tmp_path / "sbxloop.toml"
    path.write_text(
        'model = "fallback"\n[[github.repos]]\nrepo = "org/one"\n'
        '[[github.repos]]\nrepo = "org/two"\n'
    )
    cfg = load_config(tmp_path, env={})
    one = cfg.model_copy(update={"github": cfg.github.for_repo("org/one")})
    two = cfg.model_copy(update={"github": cfg.github.for_repo("org/two")})
    path.write_text(
        'model = "new"\n[agent.models]\nreview = "global-review"\n'
        '[[github.repos]]\nrepo = "org/one"\n[github.repos.agent_models]\nbuild = "one"\n'
        '[[github.repos]]\nrepo = "org/two"\n[github.repos.agent_models]\nbuild = "two"\n'
    )
    for original, repo, expected in [(one, "org/one", "one"), (two, "org/two", "two")]:
        live = refreshed_models(original)
        assert model_for_phase(live, "build", repo=repo).model == expected
        assert model_for_phase(live, "review", repo=repo).model == "global-review"
        assert original.model == "fallback"
    # Repo-less workloads clear the selected repository, even if there is a default.
    no_repo = cfg.model_copy(update={"run_model_repo": ""})
    agent = RecordingAgent()
    PhaseRunner(agent, no_repo, "r2", "workload")._agent_job(
        "brief", phase="build", permission_mode="auto", expect="text"
    )
    assert agent.jobs[0].model == "new"


@pytest.mark.parametrize("forced", [None, "forced"])
def test_resume_refreshes_original_directory_with_current_env_and_pinned_override(tmp_path, forced):
    from sbxloop.engine.engine import LoopEngine

    original = tmp_path / "original"
    original.mkdir()
    path = original / "sbxloop.toml"
    path.write_text('model = "old"\n[budgets]\nmax_tasks = 2\n')
    env = {"SBXLOOP_HOME": str(tmp_path / "home")}
    initial = load_config(original, env=env).model_copy(update={"run_model_override": forced})
    with closing(StateStore(initial.paths.state_db)) as store:
        store.create_run("r1", "the task", initial.model_dump_json())
    path.write_text('model = "live"\n[budgets]\nmax_tasks = 9\n')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "sbxloop.toml").write_text('model = "wrong-directory"\n')
    current = load_config(elsewhere, env=env | {"SBXLOOP_AGENT__MODELS__REVIEW": "env-review"})
    engine = LoopEngine(current)
    try:
        engine._rehydrate_config("r1")
        fresh = refreshed_models(engine.config)
        assert fresh.budgets.max_tasks == 2
        assert model_for_phase(fresh, "build").model == (forced or "live")
        assert model_for_phase(fresh, "review").model == (forced or "env-review")
        assert model_for_phase(fresh, "concierge").model == "live"
        assert fresh.model_source_dir == original
    finally:
        engine.store.close()


def test_persisted_session_identity_restores_only_known_matching_sessions(tmp_path):
    with closing(StateStore(tmp_path / "state.db")) as store:
        store.create_run("r1", "task")
        for phase, payload in [
            ("build", {"session_id": "known", "requested_model": "strong"}),
            ("execute", {"session_id": "operator", "requested_model": "medium"}),
            ("build", {"session_id": "legacy", "report": "old report"}),
        ]:
            store.record_phase(
                "r1",
                phase,
                task_id=None,
                attempt=1,
                status="ok",
                output_json=json.dumps(payload),
                started_at=1,
            )
        assert store.session_models("r1") == {"known": "strong", "operator": "medium"}
        agent = RecordingAgent()
        runner = PhaseRunner(
            agent, config(), "r1", "task", session_models=store.session_models("r1")
        )
        for session in ["known", "legacy"]:
            runner._agent_job(
                "prior report",
                phase="build",
                permission_mode="auto",
                expect="text",
                resume_session_id=session,
            )
        assert [job.resume_session_id for job in agent.jobs] == ["known", None]


def test_tui_nested_keys_can_be_set_and_unset_without_losing_other_roles():
    from sbxloop.configedit import keys as configkeys
    from sbxloop.configedit import toml as configtoml

    text = 'model = "fallback"\n[[github.repos]]\nrepo = "org/one"\n'
    for name in ["agent.models.build", "github.repos[0].agent_models.review"]:
        spec = configkeys.describe(name)
        assert spec.kind == "str" and spec.optional
        parts = configkeys.parse_path(name)
        text = configtoml.set_value(text, parts, "selected")
    cfg = Config.model_validate(tomllib.loads(text))
    assert model_for_phase(cfg, "review", repo="org/one").model == "selected"
    text = configtoml.unset_value(
        text, configkeys.parse_path("github.repos[0].agent_models.review")
    )
    cfg = Config.model_validate(tomllib.loads(text))
    assert model_for_phase(cfg, "review", repo="org/one").model == "fallback"
    assert model_for_phase(cfg, "build").model == "selected"


def test_cli_force_model_does_not_replace_concierge_fallback(tmp_path, monkeypatch):
    from sbxloop.cli.app import _config_with_overrides

    monkeypatch.chdir(tmp_path)
    (tmp_path / "sbxloop.toml").write_text('model = "fallback"\n[agent.models]\nbuild = "strong"\n')
    cfg = _config_with_overrides(model="forced")
    assert cfg.run_model_override == "forced"
    assert model_for_phase(cfg, "build").model == "forced"
    assert model_for_phase(cfg, "concierge").model == "fallback"


def test_concierge_refresh_rotates_only_when_its_model_changes(tmp_path):
    from tests.unit.test_daemon_concierge import make, turn

    path = tmp_path / "sbxloop.toml"
    path.write_text('[concierge]\nmodel = "first"\n')
    concierge, client, _, _, _ = make(
        tmp_path, [{"session_id": "s1"}] * 3, config={"model_source_dir": str(tmp_path)}
    )
    concierge.config._model_env = {}
    try:
        turn(concierge)
        path.write_text('[concierge]\nmodel = "first"\n[agent.models]\nbuild = "changed"\n')
        turn(concierge)
        path.write_text('[concierge]\nmodel = "second"\n')
        turn(concierge)
        assert [job.model for job in client.jobs] == ["first", "first", "second"]
        assert [job.resume_session_id for job in client.jobs] == [None, "s1", None]
    finally:
        concierge.close()


def test_concierge_model_edit_waits_for_original_provider_recovery(tmp_path, monkeypatch):
    from sbxloop.provider import ProviderRecovery
    from sbxloop.worker.client import WorkerClient
    from tests.unit.test_daemon_concierge import make, turn
    from tests.unit.test_provider_recovery import rejected

    path = tmp_path / "sbxloop.toml"
    path.write_text('[agent]\nbackend = "claude"\n[concierge]\nmodel = "first"\n')
    concierge, _, host, _, _ = make(
        tmp_path, [], config={"agent": {"backend": "claude"}, "model_source_dir": str(tmp_path)}
    )
    concierge.config._model_env = {}
    now = [1000.0]
    manager = ProviderRecovery(concierge.store, "claude", clock=lambda: now[0])
    client = WorkerClient(SimpleNamespace(name="agent"))
    client.provider_recovery = manager
    host._client = client
    jobs = []

    def submit(request):
        jobs.append(request)
        return (
            rejected()
            if len(jobs) == 1
            else JobResult(
                job_id=request.job_id, status="ok", output_text="done", session_id="recovered"
            )
        )

    monkeypatch.setattr(client, "_submit_once", lambda request, **kwargs: submit(request))
    try:
        turn(concierge)
        path.write_text('[agent]\nbackend = "claude"\n[concierge]\nmodel = "second"\n')
        turn(concierge)  # still held, no new transport
        assert len(jobs) == 1
        now[0] = manager.hold().next_at
        assert turn(concierge).text == "done"
        assert jobs[-1].model == "first" and jobs[-1].resume_session_id == "s1"
        assert turn(concierge, "next question").text == "done"
        assert jobs[-1].model == "second" and jobs[-1].resume_session_id is None
    finally:
        concierge.close()


def test_usage_attributes_shared_operator_persona_to_each_phase_and_reported_model(tmp_path):
    from sbxloop.daemon.usage import usage_for_run, usage_lines
    from sbxloop_worker.protocol import Event, EventTypes

    with closing(StateStore(tmp_path / "state.db")) as store:
        store.create_run("r1", "workload")
        for phase, model, tokens in [
            ("operator_plan", "medium", 10),
            ("operator_execute", "strong", 20),
        ]:
            store.append_event(
                Event(
                    ts=1,
                    run_id="r1",
                    job_id=phase,
                    type=EventTypes.AGENT_USAGE,
                    data={
                        "agent": "operator",
                        "agent_phase": phase,
                        "backend": "claude",
                        "model": model,
                        "requested_model": "auto",
                        "input_tokens": tokens,
                    },
                )
            )
        usage = usage_for_run(store, "r1")
        assert usage.total.input_tokens == 30 and usage.total.output_tokens is None
        assert usage.by_agent["operator"].input_tokens == 30
        assert usage.by_phase_model["operator_plan · claude · medium"].input_tokens == 10
        assert usage.by_phase_model["operator_execute · claude · strong"].input_tokens == 20
        assert all("auto" not in line for line in usage_lines(usage))


@pytest.mark.parametrize("terminal", [False, True])
def test_status_reports_model_policy_with_source_and_snapshot_scope(
    tmp_path, monkeypatch, terminal
):
    from typer.testing import CliRunner

    from sbxloop.cli.app import app

    monkeypatch.chdir(tmp_path)
    path = tmp_path / "sbxloop.toml"
    path.write_text('[agent.models]\nbuild = "initial"\n')
    cfg = load_config()
    with closing(StateStore(cfg.paths.state_db)) as store:
        store.create_run("r1", "task", cfg.model_dump_json())
        if terminal:
            store.set_run_state("r1", "completed")
    path.write_text('[agent.models]\nbuild = "live"\n')
    result = CliRunner().invoke(app, ["status", "r1", "--json"])
    assert result.exit_code == 0, result.output
    policy = json.loads(result.output)["model_policy"]
    assert policy["scope"] == ("initial policy" if terminal else "next phase")
    assert policy["roles"]["build"] == {
        "model": "initial" if terminal else "live",
        "source": "agent.models.build",
    }
    assert len(policy["roles"]) == 8 and policy["backend"] == "copilot"


def test_list_models_names_repository_and_concierge_choices(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from sbxloop.cli.app import app
    from tests.unit.test_list_models_cli import SAMPLE_MODELS, install_stub_sdk

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "300")
    (tmp_path / "sbxloop.toml").write_text(
        '[agent.models]\nbuild = "gpt-5-mini"\n[concierge]\nmodel = "small-alias"\n'
        '[[github.repos]]\nrepo = "org/one"\n[github.repos.agent_models]\nreview = "review-alias"\n'
    )
    install_stub_sdk(monkeypatch, models=SAMPLE_MODELS)
    result = CliRunner().invoke(app, ["list-models", "--repo", "org/one"])
    assert result.exit_code == 0, result.output
    assert "build: gpt-5-mini" in result.output
    assert (
        "review: review-alias" in result.output
        and "github.repos[org/one].agent_models.review" in result.output
    )
    assert "concierge: small-alias" in result.output and "not in this list" in result.output


def test_run_headline_never_labels_a_multi_model_run_with_one_model():
    from sbxloop.daemon.discord_format import agent_ident_from_config_json

    assert "initial fallback" in agent_ident_from_config_json(config().model_dump_json())["model"]
    assert (
        agent_ident_from_config_json(config(run_model_override="forced").model_dump_json())["model"]
        == "forced (run override)"
    )
    assert agent_ident_from_config_json('{"model":"legacy"}')["model"] == "legacy"


@pytest.mark.parametrize("kind,repo", [("workload", None), ("workload", "org/one"), ("code", None)])
def test_run_creation_pins_explicit_workload_repo_for_models(tmp_path, monkeypatch, kind, repo):
    from sbxloop.agentmodels import run_model_repo
    from sbxloop.engine.engine import LoopEngine

    cfg = config(home=tmp_path)
    engine = LoopEngine(cfg)
    monkeypatch.setattr(engine, "_drive", lambda *args, **kwargs: None)
    try:
        engine.start("task", run_id="r1", kind=kind, repo=repo)
        saved = Config.model_validate_json(engine.store.get_run_config("r1"))
        assert run_model_repo(saved) == (None if kind == "workload" and repo is None else "org/one")
    finally:
        engine.store.close()


@pytest.mark.parametrize("forced", [None, "forced"])
def test_dependency_preparation_uses_builder_policy(tmp_path, monkeypatch, forced):
    from sbxloop.engine.engine import LoopEngine
    from sbxloop.worker.client import WorkerClient
    from sbxloop_worker.protocol import HostToolSpec

    cfg = config(home=tmp_path, run_model_override=forced)
    engine = LoopEngine(cfg)
    client = WorkerClient(SimpleNamespace(name="agent"))
    jobs = []

    def submit(request, **kwargs):
        jobs.append(request)
        return JobResult(
            job_id=request.job_id, status="ok", output_json={"ready": True}, exit_code=0
        )

    monkeypatch.setattr(client, "_submit_once", submit)
    service = SimpleNamespace(
        kinds=["npm"],
        manifests=lambda kind: ["package.json"],
        agent=client,
        workdir="/work",
        fetch_timeout_s=30,
        fetch_tool_spec=lambda: HostToolSpec(name="fetch_dependencies", description="fetch"),
        handler=lambda **kwargs: lambda call: None,
    )
    try:
        engine.store.create_run("r1", "task", cfg.model_dump_json())
        engine._fetch_dependencies("r1", service)
        assert jobs[0].kind == "agent.session" and jobs[0].model == (forced or "strong")
        assert jobs[1].kind == "shell.check" and jobs[1].model is None
    finally:
        engine.store.close()


def test_run_bookkeeping_is_not_operator_config_drift(tmp_path):
    from sbxloop.engine.engine import LoopEngine

    cfg = config()
    saved = cfg.model_copy(
        update={"run_model_repo": "", "run_model_override": "forced", "model_source_dir": tmp_path}
    )
    assert LoopEngine._config_drift(saved, cfg) == []


@pytest.mark.parametrize("key", ["run_model_override", "run_model_repo", "model_source_dir"])
def test_operator_cannot_set_run_bookkeeping(tmp_path, key):
    (tmp_path / "sbxloop.toml").write_text(f'{key} = "injected"\n')
    with pytest.raises(ConfigError, match="bookkeeping"):
        load_config(tmp_path, env={})


@pytest.mark.parametrize("model", ["explicit-model", "auto"])
@pytest.mark.parametrize("resumed", [False, True])
def test_copilot_forwards_explicit_model_and_omits_auto(model, resumed):
    import asyncio

    from sbxloop_worker.backends.copilot import CopilotBackend

    captured = []

    class SDKClient:
        async def create_session(self, **kwargs):
            captured.append((None, kwargs))
            return "session"

        async def resume_session(self, session_id, **kwargs):
            captured.append((session_id, kwargs))
            return "session"

    job = JobRequest(
        job_id="j1",
        run_id="r1",
        kind="agent.session",
        prompt="task",
        model=model,
        permission_mode="read_only",
        available_tools=[],
        resume_session_id="previous" if resumed else None,
    )
    assert asyncio.run(CopilotBackend()._open_session(SDKClient(), job)) == "session"
    assert len(captured) == 1
    session, kwargs = captured[0]
    assert session == job.resume_session_id
    assert kwargs.get("model") == (model if model != "auto" else None)
    assert kwargs["available_tools"] == [] and callable(kwargs["on_permission_request"])
