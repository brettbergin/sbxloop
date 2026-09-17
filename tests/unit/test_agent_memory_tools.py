"""An agent's own memory tools, on the host and in a run (plan S-A5).

The contract: ``memory_tools`` gives an agent ``remember``, ``recall`` and
``forget``, written as ``agent:<slug>`` with the run and channel they came
from; ``recall`` returns only what the channel may see; a run offers the
tools only to a custom agent whose ``tools`` name ``memory``, so the
built-in team's jobs stay exactly as they were; the memory block a run was
planned with is the one it keeps, resume included; ``[memory] enabled =
false`` offers no tools at all. Expected values are written out literally.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from sbxloop.agents.assignment import AgentAssignment, MemoryBlocks, plan_assignment
from sbxloop.agents.memory import MemoryService, NoWorkspaceVisibility
from sbxloop.agents.registry import ConfigAgentRegistry
from sbxloop.agents.tools import MEMORY_TOOL_NAMES, memory_tools
from sbxloop.config import Config, MemoryConfig
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.harness import brief_for_phase
from sbxloop.engine.phases import PhaseRunner
from sbxloop.engine.skilltools import SKILL_TOOL_NAME
from sbxloop.errors import ToolRejectedError, WorkerError
from sbxloop.worker.client import WorkerClient
from sbxloop.worker.hosttools import HostToolCall
from sbxloop_worker.protocol import JobRequest
from tests.conftest import FakeSbx
from tests.unit.test_agent_assignment import RecordingAgent, run_build, run_decompose
from tests.unit.test_engine import HAPPY_TASK, Harness, task, taskgraph

ADA = {
    "slug": "ada",
    "name": "Ada",
    "instructions": "Prefer the smallest diff that meets the ask.",
    "roles": ["builder"],
}


class Clock:
    def __init__(self) -> None:
        self.now = 5_000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def service(tmp_path: Path, **cfg: object) -> MemoryService:
    return MemoryService(
        DaemonStore(tmp_path / "memory.db"),
        NoWorkspaceVisibility(),
        MemoryConfig.model_validate(cfg),
        Clock(),
    )


def tools_by_name(tools: list[Any]) -> dict[str, Any]:
    return {tool.spec.name: tool for tool in tools}


class TestMemoryTools:
    def test_the_three_tools_are_offered(self, tmp_path: Path) -> None:
        tools = memory_tools(
            service(tmp_path), "ada", channel_id="c1", run_id=None, message_id=None
        )
        assert [tool.spec.name for tool in tools] == ["remember", "recall", "forget"]
        assert frozenset({"remember", "recall", "forget"}) == MEMORY_TOOL_NAMES
        remember = tools_by_name(tools)["remember"].spec.parameters
        assert remember["required"] == ["content"]
        assert remember["properties"]["kind"]["enum"] == ["fact", "preference", "procedure"]

    def test_remember_then_recall_in_the_same_channel(self, tmp_path: Path) -> None:
        memory = service(tmp_path)
        tools = tools_by_name(
            memory_tools(memory, "ada", channel_id="c1", run_id="run-1", message_id="msg-1")
        )
        said = tools["remember"].impl({"content": "The team ships on Fridays", "kind": "fact"})
        (stored,) = memory.list("ada", channel_id="c1", include_private=True)
        assert stored.content == "The team ships on Fridays"
        assert stored.author == "agent:ada"
        assert stored.source_channel_id == "c1"
        assert stored.source_run_id == "run-1"
        assert stored.source_message_id == "msg-1"
        assert stored.id in said
        recalled = tools["recall"].impl({"query": "which team ships", "limit": 5})
        assert "The team ships on Fridays" in recalled
        assert stored.id in recalled

    def test_recall_leaves_out_another_channel_s_private_memory(self, tmp_path: Path) -> None:
        memory = service(tmp_path)
        memory.remember("ada", "Secret launch codename", channel_id="c2", author="user:u1")
        memory.remember(
            "ada", "Launch checklist lives in the wiki", channel_id=None, author="user:u1"
        )
        recall = tools_by_name(
            memory_tools(memory, "ada", channel_id="c1", run_id=None, message_id=None)
        )["recall"]
        recalled = recall.impl({"query": "launch"})
        assert "Launch checklist lives in the wiki" in recalled
        assert "Secret launch codename" not in recalled

    def test_recall_only_reads_the_agent_s_own_memories(self, tmp_path: Path) -> None:
        memory = service(tmp_path)
        memory.remember("bob", "Bob prefers tabs", channel_id="c1", author="user:u1")
        recall = tools_by_name(
            memory_tools(memory, "ada", channel_id="c1", run_id=None, message_id=None)
        )["recall"]
        assert "Bob prefers tabs" not in recall.impl({"query": "tabs"})
        assert "No memories" in recall.impl({"query": "tabs"})

    def test_forget_removes_a_visible_memory_only(self, tmp_path: Path) -> None:
        memory = service(tmp_path)
        here = memory.remember("ada", "Drop me", channel_id="c1", author="user:u1")
        elsewhere = memory.remember("ada", "Keep me", channel_id="c2", author="user:u1")
        theirs = memory.remember("bob", "Not yours", channel_id="c1", author="user:u1")
        forget = tools_by_name(
            memory_tools(memory, "ada", channel_id="c1", run_id=None, message_id=None)
        )["forget"]
        assert here.id in forget.impl({"memory_id": here.id})
        for other in (elsewhere, theirs):
            with pytest.raises(ToolRejectedError, match="not found"):
                forget.impl({"memory_id": other.id})
        left = {m.content for m in memory.list("ada", channel_id=None, include_private=True)}
        assert left == {"Keep me"}
        assert [m.content for m in memory.list("bob", channel_id="c1", include_private=True)] == [
            "Not yours"
        ]

    def test_bad_arguments_are_refused_with_a_reason(self, tmp_path: Path) -> None:
        tools = tools_by_name(
            memory_tools(service(tmp_path), "ada", channel_id="c1", run_id=None, message_id=None)
        )
        with pytest.raises(ToolRejectedError, match="text"):
            tools["remember"].impl({"content": "   "})
        with pytest.raises(ToolRejectedError, match="kind"):
            tools["remember"].impl({"content": "x", "kind": "gossip"})
        with pytest.raises(ToolRejectedError, match="memory_id"):
            tools["forget"].impl({})

    def test_content_is_capped(self, tmp_path: Path) -> None:
        memory = service(tmp_path, max_item_chars=5)
        tools = tools_by_name(
            memory_tools(memory, "ada", channel_id=None, run_id=None, message_id=None)
        )
        assert tools["remember"].spec.parameters["properties"]["content"]["maxLength"] == 5
        tools["remember"].impl({"content": "abcdefgh"})
        assert [m.content for m in memory.list("ada", channel_id=None, include_private=True)] == [
            "abcde"
        ]

    def test_a_read_only_turn_only_recalls(self, tmp_path: Path) -> None:
        tools = memory_tools(
            service(tmp_path), "ada", channel_id="c1", run_id=None, message_id=None, writable=False
        )
        assert [tool.spec.name for tool in tools] == ["recall"]

    def test_disabled_memory_offers_no_tools(self, tmp_path: Path) -> None:
        memory = service(tmp_path, enabled=False)
        assert memory_tools(memory, "ada", channel_id="c1", run_id=None, message_id=None) == []


def cfg(*agents: dict[str, Any], **extra: Any) -> Config:
    return Config.model_validate({"model": "fallback", "agents": list(agents), **extra})


def plan(config: Config, memory: MemoryService | None, channel_id: str | None) -> AgentAssignment:
    return plan_assignment(
        ConfigAgentRegistry(config),
        kind="code",
        lead=None,
        requested={"builder": "ada"},
        memory=memory,
        channel_id=channel_id,
    )


class TestRunTools:
    def test_an_agent_listing_memory_gets_the_tools_and_they_write_for_the_run(
        self, tmp_path: Path
    ) -> None:
        memory = service(tmp_path)
        config = cfg({**ADA, "tools": ["memory"]})
        agent = run_build(config, plan(config, memory, "chan-7"), memory=memory)
        (job,) = agent.jobs
        assert sorted(t.name for t in job.host_tools) == ["forget", "recall", "remember"]
        answer = agent.kwargs[0]["tool_handler"]
        said = answer(
            HostToolCall(call_id="c1", name="remember", arguments={"content": "Use feature flags"})
        )
        assert said.ok, said
        (stored,) = memory.list("ada", channel_id="chan-7", include_private=False)
        assert (stored.author, stored.source_run_id, stored.source_channel_id) == (
            "agent:ada",
            "r1",
            "chan-7",
        )
        recalled = answer(HostToolCall(call_id="c2", name="recall", arguments={"query": "flags"}))
        assert recalled.ok and "Use feature flags" in recalled.text
        refused = answer(HostToolCall(call_id="c3", name="forget", arguments={"memory_id": "nope"}))
        assert not refused.ok and "not found" in (refused.error or "")

    def test_memory_rides_beside_the_agent_s_other_tools(self, tmp_path: Path) -> None:
        memory = service(tmp_path)
        config = cfg({**ADA, "tools": ["memory", "load_skill"]})
        agent = run_build(config, plan(config, memory, None), memory=memory)
        names = sorted(t.name for t in agent.jobs[0].host_tools)
        assert names == sorted(["forget", "recall", "remember", SKILL_TOOL_NAME])

    @pytest.mark.parametrize(
        "spec",
        [ADA, {**ADA, "tools": ["load_skill"]}],
        ids=["no-tool-list", "list-without-memory"],
    )
    def test_an_agent_not_listing_memory_gets_no_memory_tools(
        self, tmp_path: Path, spec: dict[str, Any]
    ) -> None:
        memory = service(tmp_path)
        config = cfg(spec)
        agent = run_build(config, plan(config, memory, None), memory=memory)
        assert not {t.name for t in agent.jobs[0].host_tools} & MEMORY_TOOL_NAMES

    def test_disabled_memory_offers_a_run_no_tools(self, tmp_path: Path) -> None:
        memory = service(tmp_path, enabled=False)
        config = cfg({**ADA, "tools": ["memory"]})
        agent = run_build(config, plan(config, memory, None), memory=memory)
        assert agent.jobs[0].host_tools == []
        assert agent.kwargs[0]["tool_handler"] is None

    def test_the_default_team_submits_the_same_jobs_with_memory_wired(self, tmp_path: Path) -> None:
        memory = service(tmp_path)
        config = cfg()
        default = plan_assignment(
            ConfigAgentRegistry(config),
            kind="code",
            lead=None,
            requested={},
            memory=memory,
            channel_id="c1",
        )
        assert default.is_default()
        for runner in (run_build, run_decompose):
            plain = runner(config, None)
            wired = (
                run_build(config, default, memory=memory)
                if runner is run_build
                else _decompose_with(config, default, memory)
            )
            strip = {"job_id"}
            assert [j.model_dump(exclude=strip) for j in plain.jobs] == [
                j.model_dump(exclude=strip) for j in wired.jobs
            ]

    def test_a_built_in_with_memories_is_told_them(self, tmp_path: Path) -> None:
        memory = service(tmp_path)
        memory.remember("builder", "Run the formatter first", channel_id="c1", author="user:u1")
        config = cfg()
        planned = plan_assignment(
            ConfigAgentRegistry(config),
            kind="code",
            lead=None,
            requested={},
            memory=memory,
            channel_id="c1",
        )
        assert "Run the formatter first" in planned.agents["builder"].memory_block
        assert planned.agents["planner"].memory_block == ""
        build = run_build(config, planned, memory=memory).jobs[0]
        assert build.system_message == (
            brief_for_phase(config, "build", None)
            + "\n\n## What you remember\n- (fact) Run the formatter first\n"
        )
        # A built-in has no tool list, so it is given no memory tools.
        assert not {t.name for t in build.host_tools} & MEMORY_TOOL_NAMES


def _decompose_with(config: Config, assignment: AgentAssignment, memory: MemoryService) -> Any:
    graph = {"tasks": [{"id": "t1", "title": "Write", "verify_commands": ["test -f x"]}]}
    agent = RecordingAgent([graph])
    runner = PhaseRunner(agent, config, "r1", "outcome", assignment=assignment, memory=memory)  # type: ignore[arg-type]
    runner.decompose()
    return agent


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


def test_the_memory_snapshot_holds_across_a_resume(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[JobRequest] = []
    real_submit = WorkerClient.submit

    def recording_submit(self: WorkerClient, job: JobRequest, **kwargs: Any) -> Any:
        submitted.append(job)
        return real_submit(self, job, **kwargs)

    monkeypatch.setattr(WorkerClient, "submit", recording_submit)
    memory = service(tmp_path)
    memory.remember("ada", "Known before the run", channel_id="chan-1", author="user:u1")
    harness.script([taskgraph(task("t1")), {"fail": "sandbox exploded"}])
    engine = harness.engine(agents=[{**ADA, "tools": ["memory"]}])
    engine.memory = memory
    assignment = plan(engine.config, memory, "chan-1")
    with pytest.raises(WorkerError, match="sandbox exploded"):
        engine.start("crashy run", assignment=assignment)
    run_id = engine.store.list_runs()[0].run_id

    # Learned while the run was down: the resumed run keeps its snapshot.
    memory.remember("ada", "Learned after the start", channel_id="chan-1", author="user:u1")
    submitted.clear()
    harness.script([*HAPPY_TASK])
    engine2 = harness.engine()
    engine2.memory = memory
    assert engine2.resume(run_id).state == "completed"
    builds = [
        job
        for job in submitted
        if job.kind == "agent.session" and "Prefer the smallest diff" in (job.system_message or "")
    ]
    assert builds
    for job in builds:
        assert "Known before the run" in (job.system_message or "")
        assert "Learned after the start" not in (job.system_message or "")
        assert {"remember", "recall", "forget"} <= {tool.name for tool in job.host_tools}


def test_the_stored_assignment_keeps_the_memory_it_was_planned_with(tmp_path: Path) -> None:
    """What a resume reads back is the JSON the run was started with."""
    memory = service(tmp_path)
    memory.remember("ada", "Known before the run", channel_id="chan-1", author="user:u1")
    config = cfg({**ADA, "tools": ["memory"]})
    stored = plan(config, memory, "chan-1").to_json()
    memory.remember("ada", "Learned after the start", channel_id="chan-1", author="user:u1")
    resumed = AgentAssignment.from_json(stored)
    system = run_build(config, resumed, memory=memory).jobs[0].system_message or ""
    assert "Known before the run" in system
    assert "Learned after the start" not in system
    # The tools still read the live store.
    assert "Learned after the start" in memory.prompt_block("ada", channel_id="chan-1")


def test_a_run_is_told_nothing_new_when_ada_has_no_memories(tmp_path: Path) -> None:
    memory = service(tmp_path)
    config = cfg(ADA)
    with_memory = run_build(config, plan(config, memory, "c1"), memory=memory).jobs[0]
    without = run_build(config, plan(config, None, "c1")).jobs[0]
    assert with_memory.system_message == without.system_message


def test_the_memory_service_is_the_memory_source_a_plan_declares(tmp_path: Path) -> None:
    """``plan_assignment(memory=...)`` is handed the real ``MemoryService``,
    so ``MemoryBlocks`` has to describe it: every parameter the protocol
    declares is one the service accepts, under the same name and kind, and
    the protocol promises no default the service does not have. Otherwise
    the protocol only happens to work because the one call site passes the
    agent positionally, and a caller writing the call out by name is
    refused."""
    declared = inspect.signature(MemoryBlocks.prompt_block).parameters
    accepted = inspect.signature(MemoryService.prompt_block).parameters
    for name, parameter in declared.items():
        assert name in accepted, f"MemoryService.prompt_block takes no {name!r}"
        assert accepted[name].kind == parameter.kind, name
        if parameter.default is not inspect.Parameter.empty:
            assert accepted[name].default is not inspect.Parameter.empty, name

    memory = service(tmp_path)
    memory.remember("ada", "Prefers small diffs", channel_id=None, author="user:u1")
    blocks: MemoryBlocks = memory
    call: dict[str, Any] = dict.fromkeys(name for name in declared if name != "self")
    call[next(iter(call))] = "ada"
    assert "Prefers small diffs" in blocks.prompt_block(**call)
