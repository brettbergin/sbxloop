"""A run's named-agent assignment: which agent takes each phase, and what
that changes in the jobs a run submits.

The contract (plan S-A4): ``plan_assignment`` picks a requested agent per
role when it exists, is enabled and declares the role, and the built-in for
that role otherwise; ``binding_for`` answers a phase (a task's own assignee
first for the working phases); a default assignment is indistinguishable
from no assignment in every prompt, tool list and event; a custom binding
adds its persona and memory to the system message, narrows host tools and
credentials, and supplies a model below the repository's per-phase model.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from sbxloop.agentmodels import model_for_phase
from sbxloop.agents.assignment import AgentAssignment, AgentBinding, plan_assignment
from sbxloop.agents.registry import ConfigAgentRegistry
from sbxloop.config import Config
from sbxloop.engine.harness import brief_for_phase
from sbxloop.engine.model import TaskRecord, TaskSpec
from sbxloop.engine.phases import PhaseRunner
from sbxloop.engine.service import TOOL_NAME as CALL_SERVICE
from sbxloop.engine.skilltools import SKILL_TOOL_NAME
from sbxloop.worker.hosttools import HostToolCall
from sbxloop_worker.protocol import HostToolResponse, HostToolSpec, JobRequest, JobResult

ADA = {
    "slug": "ada",
    "name": "Ada",
    "instructions": "Prefer the smallest diff that meets the ask.",
    "roles": ["builder"],
}


def cfg(*agents: dict[str, Any], **extra: Any) -> Config:
    return Config.model_validate({"model": "fallback", "agents": list(agents), **extra})


def registry(*agents: dict[str, Any], **extra: Any) -> ConfigAgentRegistry:
    return ConfigAgentRegistry(cfg(*agents, **extra))


class Memory:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str | None]] = []

    def prompt_block(self, agent_slug: str, *, channel_id: str | None = None) -> str:
        """Only Ada remembers anything."""
        self.asked.append((agent_slug, channel_id))
        if agent_slug != "ada":
            return ""
        return f"\n\n## What you remember\n\n- {agent_slug} likes tidy commits\n"


class TestPlanAssignment:
    def test_no_request_is_the_built_in_team_and_is_default(self) -> None:
        plan = plan_assignment(registry(), kind="code", lead=None, requested={}, channel_id=None)
        assert plan.lead == "concierge"
        assert plan.roles["planner"] == "planner"
        assert plan.roles["builder"] == "builder"
        assert plan.roles["critic"] == "critic"
        for slug in plan.roles.values():
            binding = plan.agents[slug]
            assert binding.persona == "" and binding.memory_block == ""
            assert binding.model is None and binding.tools is None
            assert binding.credentials == ()
        assert plan.is_default()

    def test_a_requested_agent_that_declares_the_role_takes_it(self) -> None:
        plan = plan_assignment(
            registry(ADA), kind="code", lead=None, requested={"builder": "ada"}, channel_id="c1"
        )
        assert plan.roles["builder"] == "ada"
        ada = plan.agents["ada"]
        assert (ada.slug, ada.name, ada.role) == ("ada", "Ada", "builder")
        assert "Prefer the smallest diff" in ada.persona
        assert plan.channel_id == "c1"
        assert not plan.is_default()

    @pytest.mark.parametrize(
        "agents,requested",
        [
            ((), "nobody"),  # unknown
            (({**ADA, "enabled": False},), "ada"),  # disabled
            (({**ADA, "roles": ["critic"]},), "ada"),  # does not declare the role
        ],
    )
    def test_an_unusable_request_falls_back_to_the_built_in(
        self, agents: tuple[dict[str, Any], ...], requested: str
    ) -> None:
        plan = plan_assignment(
            registry(*agents),
            kind="code",
            lead=None,
            requested={"builder": requested},
            channel_id=None,
        )
        assert plan.roles["builder"] == "builder"
        assert plan.is_default()

    def test_the_lead_defaults_to_angie_and_takes_a_lead_agent(self) -> None:
        boss = {"slug": "boss", "name": "Boss", "roles": ["lead"]}
        plain = registry(boss, ADA)

        def lead(requested: str | None) -> str:
            return plan_assignment(
                plain, kind="code", lead=requested, requested={}, channel_id=None
            ).lead

        assert lead(None) == "concierge"
        assert lead("boss") == "boss"
        # Ada takes no lead role: Angie leads.
        assert lead("ada") == "concierge"

    def test_memory_fills_each_binding_and_none_leaves_it_empty(self) -> None:
        memory = Memory()
        plan = plan_assignment(
            registry(ADA),
            kind="code",
            lead=None,
            requested={"builder": "ada"},
            memory=memory,
            channel_id="c9",
        )
        assert "ada likes tidy commits" in plan.agents["ada"].memory_block
        assert ("ada", "c9") in memory.asked
        assert not plan.is_default()
        bare = plan_assignment(
            registry(ADA), kind="code", lead=None, requested={"builder": "ada"}, channel_id="c9"
        )
        assert bare.agents["ada"].memory_block == ""

    def test_an_adjusted_built_in_model_is_not_default(self) -> None:
        plan = plan_assignment(
            registry({"slug": "builder", "model": "big"}),
            kind="code",
            lead=None,
            requested={},
            channel_id=None,
        )
        assert plan.agents["builder"].model == "big"
        assert not plan.is_default()

    def test_a_tool_run_has_no_role_to_fill(self) -> None:
        plan = plan_assignment(
            registry(ADA), kind="tool", lead=None, requested={"builder": "ada"}, channel_id=None
        )
        assert dict(plan.roles) == {}
        assert plan.binding_for("build") is None


class TestBindingFor:
    def plan(self) -> AgentAssignment:
        base = plan_assignment(
            registry(ADA, {"slug": "bob", "name": "Bob", "roles": ["builder"]}),
            kind="code",
            lead=None,
            requested={"builder": "ada"},
            channel_id=None,
        )
        bob = plan_assignment(
            registry({"slug": "bob", "name": "Bob", "roles": ["builder"]}),
            kind="code",
            lead=None,
            requested={"builder": "bob"},
            channel_id=None,
        ).agents["bob"]
        return AgentAssignment(
            lead=base.lead,
            roles=base.roles,
            agents={**base.agents, "bob": bob},
            tasks={"t2": "bob"},
        )

    def test_phases_resolve_through_their_role(self) -> None:
        plan = self.plan()
        assert plan.binding_for("decompose").slug == "planner"  # type: ignore[union-attr]
        assert plan.binding_for("build").slug == "ada"  # type: ignore[union-attr]
        assert plan.binding_for("review").slug == "critic"  # type: ignore[union-attr]
        assert plan.binding_for("reauthor_verify").slug == "critic"  # type: ignore[union-attr]

    def test_a_task_assignee_takes_that_task_s_working_phase(self) -> None:
        plan = self.plan()
        assert plan.binding_for("build", "t2").slug == "bob"  # type: ignore[union-attr]
        assert plan.binding_for("build", "t1").slug == "ada"  # type: ignore[union-attr]

    def test_steer_is_the_planner_whatever_the_task(self) -> None:
        plan = self.plan()
        assert plan.binding_for("steer", "t2").slug == "planner"  # type: ignore[union-attr]
        assert plan.binding_for("steer").slug == "planner"  # type: ignore[union-attr]

    def test_the_critic_judges_a_task_it_did_not_do(self) -> None:
        plan = self.plan()
        assert plan.binding_for("operator_judge", "t2").slug == "critic"  # type: ignore[union-attr]

    def test_an_unknown_phase_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="no harness role"):
            self.plan().binding_for("dance")


class TestJson:
    def test_round_trip_is_equal_and_stable(self) -> None:
        plan = plan_assignment(
            registry({**ADA, "tools": ["load_skill"], "model": "m1"}),
            kind="workload",
            lead=None,
            requested={"builder": "ada"},
            memory=Memory(),
            channel_id="chan",
        )
        plan = AgentAssignment(
            lead=plan.lead,
            roles=plan.roles,
            agents=plan.agents,
            tasks={"t1": "ada"},
            channel_id="chan",
            origin_agent="planner",
            chain_depth=2,
        )
        text = plan.to_json()
        back = AgentAssignment.from_json(text)
        assert back == plan
        assert back.to_json() == text
        assert list(json.loads(text)) == [
            "lead",
            "roles",
            "agents",
            "tasks",
            "channel_id",
            "origin_agent",
            "chain_depth",
        ]
        assert back.agents["ada"].tools == frozenset({"load_skill"})

    def test_a_binding_without_narrowing_survives_as_none(self) -> None:
        binding = AgentBinding(
            slug="x",
            name="X",
            role="builder",
            model=None,
            persona="",
            memory_block="",
            tools=None,
            credentials=(),
            revision=0,
        )
        plan = AgentAssignment(lead="concierge", roles={"builder": "x"}, agents={"x": binding})
        assert AgentAssignment.from_json(plan.to_json()).agents["x"].tools is None


class TestModelPrecedence:
    def config(self, **extra: Any) -> Config:
        return Config.model_validate(
            {
                "model": "fallback",
                "agent": {"models": {"build": "role-model"}},
                "github": {"repos": [{"repo": "org/one", "agent_models": {"build": "repo-model"}}]},
                **extra,
            }
        )

    def test_the_agent_model_beats_the_role_model_and_the_fallback(self) -> None:
        chosen = model_for_phase(self.config(), "build", agent_model="ada-model", agent_slug="ada")
        assert (chosen.model, chosen.source) == ("ada-model", "agents[ada].model")
        plain = Config.model_validate({"model": "fallback"})
        chosen = model_for_phase(plain, "review", agent_model="ada-model", agent_slug="ada")
        assert (chosen.model, chosen.source) == ("ada-model", "agents[ada].model")

    def test_the_repository_phase_model_beats_the_agent_model(self) -> None:
        chosen = model_for_phase(
            self.config(), "build", repo="org/one", agent_model="ada-model", agent_slug="ada"
        )
        assert chosen.model == "repo-model"

    def test_the_run_override_beats_everything(self) -> None:
        chosen = model_for_phase(
            self.config(run_model_override="forced"),
            "build",
            repo="org/one",
            agent_model="ada-model",
            agent_slug="ada",
        )
        assert (chosen.model, chosen.source) == ("forced", "--model")

    def test_no_agent_model_changes_nothing(self) -> None:
        chosen = model_for_phase(self.config(), "build", agent_model=None, agent_slug="ada")
        assert (chosen.model, chosen.source) == ("role-model", "agent.models.build")


class RecordingAgent:
    def __init__(self, outputs: list[Any] | None = None) -> None:
        self.jobs: list[JobRequest] = []
        self.kwargs: list[dict[str, Any]] = []
        self.outputs = list(outputs or [])

    def submit(self, job: JobRequest, **kwargs: Any) -> JobResult:
        self.jobs.append(job)
        self.kwargs.append(kwargs)
        return JobResult(
            job_id=job.job_id,
            status="ok",
            session_id=f"s{len(self.jobs)}",
            output_text="done",
            output_json=self.outputs.pop(0) if self.outputs else None,
        )


def service_spec(*names: str) -> HostToolSpec:
    return HostToolSpec(
        name=CALL_SERVICE,
        description="call a service",
        parameters={
            "type": "object",
            "properties": {"credential": {"type": "string", "enum": list(names)}},
        },
    )


def custom_plan(config: Config, **overrides: Any) -> AgentAssignment:
    return plan_assignment(
        ConfigAgentRegistry(config),
        kind="code",
        lead=None,
        requested={"builder": "ada"},
        memory=overrides.pop("memory", None),
        channel_id=None,
    )


TASK = TaskRecord(spec=TaskSpec(id="t1", title="Write the file"))


def run_build(config: Config, assignment: AgentAssignment | None, **kwargs: Any) -> RecordingAgent:
    agent = RecordingAgent()
    runner = PhaseRunner(agent, config, "r1", "outcome", assignment=assignment, **kwargs)  # type: ignore[arg-type]
    runner.build(TASK)
    return agent


def run_decompose(config: Config, assignment: AgentAssignment | None) -> RecordingAgent:
    graph = {"tasks": [{"id": "t1", "title": "Write", "verify_commands": ["test -f x"]}]}
    agent = RecordingAgent([graph])
    runner = PhaseRunner(agent, config, "r1", "outcome", assignment=assignment)  # type: ignore[arg-type]
    runner.decompose()
    return agent


class TestPhaseRunner:
    def test_a_default_assignment_submits_the_same_jobs_as_none(self) -> None:
        config = cfg()
        default = plan_assignment(
            ConfigAgentRegistry(config), kind="code", lead=None, requested={}, channel_id=None
        )
        for runner in (run_build, run_decompose):
            plain, assigned = runner(config, None), runner(config, default)
            strip = {"job_id"}
            assert [j.model_dump(exclude=strip) for j in plain.jobs] == [
                j.model_dump(exclude=strip) for j in assigned.jobs
            ]

            def shape(kwargs: dict[str, Any]) -> dict[str, Any]:
                # Handlers are closures: what matters is whether one is set.
                return {k: (v is not None) if k == "tool_handler" else v for k, v in kwargs.items()}

            assert [shape(kw) for kw in plain.kwargs] == [shape(kw) for kw in assigned.kwargs]
            assert all("agent_identity" not in kw for kw in assigned.kwargs)

    def test_a_custom_binding_adds_persona_and_memory_to_its_own_system_message(self) -> None:
        config = cfg(ADA)
        plan = custom_plan(config, memory=Memory())
        build = run_build(config, plan).jobs[0]
        expected_head = brief_for_phase(config, "build", None)
        assert build.system_message is not None
        assert build.system_message.startswith(expected_head)
        assert "Prefer the smallest diff" in build.system_message
        assert "ada likes tidy commits" in build.system_message
        assert "Prefer the smallest diff" not in build.prompt
        # The planner in the same run is a built-in: its session is unchanged.
        decompose = run_decompose(config, plan).jobs[0]
        assert decompose.system_message == brief_for_phase(config, "decompose", None)

    def test_a_custom_assignment_stamps_who_submitted_each_job(self) -> None:
        config = cfg(ADA)
        plan = custom_plan(config)
        assert run_build(config, plan).kwargs[0]["agent_identity"] == {
            "agent_slug": "ada",
            "agent_name": "Ada",
        }
        assert run_decompose(config, plan).kwargs[0]["agent_identity"] == {
            "agent_slug": "planner",
            "agent_name": "Planner",
        }

    def test_the_agent_model_reaches_the_job(self) -> None:
        config = cfg({**ADA, "model": "ada-model"})
        agent = run_build(config, custom_plan(config))
        assert agent.jobs[0].model == "ada-model"
        assert agent.kwargs[0]["model_source"] == "agents[ada].model"

    def test_tools_narrow_to_the_agent_s_list(self) -> None:
        def handler(call: HostToolCall) -> HostToolResponse:
            return HostToolResponse(call_id=call.call_id, ok=True, text="ok")

        tools = {"host_tools": [service_spec("weather")], "tool_handler": handler}
        config = cfg({**ADA, "tools": ["call_service"]})
        narrowed = run_build(config, custom_plan(config), **tools).jobs[0]
        assert [t.name for t in narrowed.host_tools] == [CALL_SERVICE]
        config = cfg({**ADA, "tools": ["load_skill"]})
        narrowed = run_build(config, custom_plan(config), **tools)
        assert [t.name for t in narrowed.jobs[0].host_tools] == [SKILL_TOOL_NAME]
        assert "Services you may call" not in narrowed.jobs[0].prompt
        default = run_build(cfg(ADA), None, **tools).jobs[0]
        assert sorted(t.name for t in default.host_tools) == sorted([CALL_SERVICE, SKILL_TOOL_NAME])

    def test_a_tool_left_out_of_the_agent_s_list_is_refused_when_called(self) -> None:
        served: list[str] = []

        def handler(call: HostToolCall) -> HostToolResponse:
            served.append(call.name)
            return HostToolResponse(call_id=call.call_id, ok=True, text="ok")

        fetch = HostToolSpec(
            name="fetch_dependencies",
            description="fetch dependencies",
            parameters={"type": "object", "properties": {}},
        )
        tools = {"host_tools": [service_spec("weather"), fetch], "tool_handler": handler}
        config = cfg({**ADA, "tools": ["load_skill"]})
        answer = run_build(config, custom_plan(config), **tools).kwargs[0]["tool_handler"]
        refused = answer(
            HostToolCall(call_id="c1", name=CALL_SERVICE, arguments={"credential": "weather"})
        )
        assert not refused.ok and CALL_SERVICE in (refused.error or "")
        assert not answer(HostToolCall(call_id="c2", name="fetch_dependencies", arguments={})).ok
        assert served == []

        config = cfg({**ADA, "tools": ["fetch_dependencies"]})
        answer = run_build(config, custom_plan(config), **tools).kwargs[0]["tool_handler"]
        refused = answer(
            HostToolCall(call_id="c3", name=CALL_SERVICE, arguments={"credential": "weather"})
        )
        assert not refused.ok
        assert answer(HostToolCall(call_id="c4", name="fetch_dependencies", arguments={})).ok
        assert served == ["fetch_dependencies"]

        # With no list, every run tool is still answered.
        answer = run_build(cfg(ADA), custom_plan(cfg(ADA)), **tools).kwargs[0]["tool_handler"]
        assert answer(
            HostToolCall(call_id="c5", name=CALL_SERVICE, arguments={"credential": "weather"})
        ).ok

    def test_call_service_is_narrowed_to_the_agent_s_credentials(self) -> None:
        calls: list[str] = []

        def handler(call: HostToolCall) -> HostToolResponse:
            calls.append(str(call.arguments.get("credential")))
            return HostToolResponse(call_id=call.call_id, ok=True, text="ok")

        credentials = [
            {"name": "weather", "env": "WEATHER_KEY", "host": "w.example.com"},
            {"name": "stocks", "env": "STOCKS_KEY", "host": "s.example.com"},
        ]
        config = cfg({**ADA, "credentials": ["weather"]}, credentials=credentials)
        agent = run_build(
            config,
            custom_plan(config),
            host_tools=[service_spec("weather", "stocks")],
            tool_handler=handler,
        )
        (spec,) = [t for t in agent.jobs[0].host_tools if t.name == CALL_SERVICE]
        assert spec.parameters["properties"]["credential"]["enum"] == ["weather"]
        answer = agent.kwargs[0]["tool_handler"]
        refused = answer(
            HostToolCall(call_id="c1", name=CALL_SERVICE, arguments={"credential": "stocks"})
        )
        assert not refused.ok and "stocks" in (refused.error or "")
        allowed = answer(
            HostToolCall(call_id="c2", name=CALL_SERVICE, arguments={"credential": "weather"})
        )
        assert allowed.ok
        assert calls == ["weather"]

    def test_a_review_checkpoint_changes_with_the_critic_s_persona(self, tmp_path: Any) -> None:
        from contextlib import closing

        from sbxloop.engine.store import StateStore

        critic = {
            "slug": "carla",
            "name": "Carla",
            "roles": ["critic"],
            "instructions": "Be strict.",
        }
        config = cfg(critic)
        plan = plan_assignment(
            ConfigAgentRegistry(config),
            kind="code",
            lead=None,
            requested={"critic": "carla"},
            channel_id=None,
        )
        keys = []
        with closing(StateStore(tmp_path / "state.db")) as store:
            store.create_run("r1", "outcome")
            for assignment in (None, plan):
                # An invalid review is checkpointed; the key is the identity.
                agent = RecordingAgent([{"verdict": "nonsense"}] * 3)
                runner = PhaseRunner(
                    agent,  # type: ignore[arg-type]
                    config,
                    "r1",
                    "outcome",
                    store=store,
                    assignment=assignment,
                )
                with pytest.raises(Exception):  # noqa: B017 - either repair failure
                    runner.review(
                        diff="d", pr_number=1, round=1, tasks=[], history="", refuted=set()
                    )
                keys.append(
                    {
                        row.task_id
                        for row in store.phase_attempts("r1")
                        if row.phase == "review_response_repair"
                    }
                )
        assert keys[0] and keys[1] - keys[0]
        assert "Be strict." in (agent.jobs[0].system_message or "")
