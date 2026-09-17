"""Role assignment at admission: who leads a run and who takes each role.

Admission may name a lead, an agent per run role and the channel the work
belongs to. The service refuses an agent that does not exist, is archived or
does not declare the role; dispatch plans the assignment from what was asked
and hands it to the engine, a later attempt reuses the assignment the item
already has, and an issue discovered by polling runs with the built-in team.

Expected values come from the request (the slugs and roles it names) and from
the built-in team's slugs, never from the planner under test.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from alembic import command

from sbxloop.agents.assignment import AgentAssignment
from sbxloop.agents.definition import AgentSpec
from sbxloop.agents.memory import MemoryService, WorkspaceChannelVisibility
from sbxloop.agents.registry import DbAgentRegistry
from sbxloop.config import Config
from sbxloop.daemon.controls import ControlError, ControlService, Principal
from sbxloop.daemon.controls.intake import IssueAdmission, WorkloadAdmission
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import _admission_key
from sbxloop.db import ensure_schema, open_engine
from sbxloop.db.schema import _config as alembic_config
from sbxloop.engine.model import RunResult
from sbxloop.events import EventBus
from tests.unit.test_daemon_loop import Harness, gh_item

CLIENT = Principal(
    kind="client",
    id="cli_a",
    display="reporter",
    via="api",
    capabilities=frozenset({"items:create", "runs:read"}),
)

#: The built-in team, by slug, as the agent registry contract names it.
BUILTIN_ROLES = {
    "planner": "planner",
    "builder": "builder",
    "critic": "critic",
    "operator": "operator",
}
ANGIE = "concierge"


def _config(tmp_path: Path) -> Config:
    return Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "github": {"repo": "o/r"},
            "workloads": [{"name": "research", "sinks": ["chat"]}],
        }
    )


def _agent(harness: Harness, slug: str, roles: list[str], *, archived: bool = False) -> None:
    registry = DbAgentRegistry(harness.config, harness.dstore, clock=harness.clock)
    registry.create(
        AgentSpec.model_validate(
            {"slug": slug, "name": slug.title(), "instructions": f"Be {slug}.", "roles": roles}
        ),
        by="test",
    )
    if archived:
        registry.archive(slug, by="test")


def _remember(harness: Harness, agent: str, content: str, *, channel_id: str | None) -> None:
    """Keep ``content`` for ``agent`` the way the daemon's own memory
    service does, so a planned run reads it from the same store."""
    MemoryService(
        harness.dstore,
        WorkspaceChannelVisibility(harness.dstore),
        harness.config.memory,
        harness.clock,
    ).remember(agent, content, channel_id=channel_id, author="user:tester")


class Capturing(Harness):
    """The loop harness, recording the item each dispatch handed the runner."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path, _config(tmp_path))
        self.dispatched: list[WorkItem] = []

    def runner(
        self, item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
    ) -> RunResult:
        self.dispatched.append(item)
        return super().runner(item, cfg, run_id, bus, resume)


def _assignment(item: WorkItem) -> AgentAssignment:
    assert item.assignment_json is not None
    return AgentAssignment.from_json(item.assignment_json)


class TestAdmissionValidation:
    def _admit(self, harness: Harness, **fields: Any) -> WorkItem:
        request = WorkloadAdmission(ask="Write the brief", profile="research", **fields)
        return ControlService(harness.loop).admit(CLIENT, request).item

    def test_named_agents_ride_the_item(self, tmp_path: Path) -> None:
        harness = Capturing(tmp_path)
        _agent(harness, "scout", ["planner"])
        _agent(harness, "chief", ["lead"])
        item = self._admit(
            harness, lead="chief", roles={"planner": "scout"}, channel_id="chn_brief"
        )
        stored = harness.dstore.get(item.item_id)
        assert stored is not None
        assert stored.lead_agent == "chief"
        assert stored.channel_id == "chn_brief"
        assert stored.assignment_json is not None
        assert json.loads(stored.assignment_json)["roles"] == {"planner": "scout"}

    @pytest.mark.parametrize(
        ("fields", "fragment"),
        [
            ({"roles": {"planner": "ghost"}}, "ghost"),
            ({"lead": "ghost"}, "ghost"),
            ({"roles": {"planner": "retired"}}, "retired"),
            ({"roles": {"critic": "scout"}}, "critic"),
            ({"lead": "scout"}, "lead"),
            ({"roles": {"janitor": "scout"}}, "janitor"),
        ],
    )
    def test_unusable_agents_are_refused_by_name(
        self, tmp_path: Path, fields: dict[str, Any], fragment: str
    ) -> None:
        harness = Capturing(tmp_path)
        _agent(harness, "scout", ["planner"])
        _agent(harness, "retired", ["planner"], archived=True)
        with pytest.raises(ControlError) as refused:
            self._admit(harness, **fields)
        assert refused.value.code == "invalid_argument"
        assert fragment in refused.value.message
        assert harness.dstore.items() == []

    def test_an_issue_admission_carries_the_same_fields(self, tmp_path: Path) -> None:
        harness = Capturing(tmp_path)
        _agent(harness, "scout", ["planner"])

        def admit(repo: str, number: str, kind: Any) -> WorkItem:
            return gh_item(number, repo=repo, kind=kind)

        harness.source.admit = admit  # type: ignore[attr-defined]
        request = IssueAdmission(
            repository="o/r", number=7, roles={"planner": "scout"}, channel_id="chn_x"
        )
        item = ControlService(harness.loop).admit(CLIENT, request).item
        assert item.channel_id == "chn_x"
        assert item.assignment_json is not None
        assert json.loads(item.assignment_json)["roles"] == {"planner": "scout"}


class TestDispatch:
    def test_the_planned_assignment_reaches_the_engine_and_the_item(self, tmp_path: Path) -> None:
        harness = Capturing(tmp_path)
        _agent(harness, "scout", ["planner"])
        item = (
            ControlService(harness.loop)
            .admit(
                CLIENT,
                WorkloadAdmission(
                    ask="Write the brief", roles={"planner": "scout"}, channel_id="chn_brief"
                ),
            )
            .item
        )
        harness.source.items = [item]
        harness.outcomes = ["completed"]
        harness.loop.tick()
        (dispatched,) = harness.dispatched
        planned = _assignment(dispatched)
        assert dict(planned.roles) == {**BUILTIN_ROLES, "planner": "scout"}
        assert planned.lead == ANGIE
        assert planned.channel_id == "chn_brief"
        stored = harness.dstore.get(item.item_id)
        assert stored is not None and stored.assignment_json == dispatched.assignment_json

    def test_a_later_attempt_reuses_the_stored_assignment(self, tmp_path: Path) -> None:
        harness = Capturing(tmp_path)
        _agent(harness, "scout", ["planner"])
        item = (
            ControlService(harness.loop)
            .admit(CLIENT, WorkloadAdmission(ask="Write the brief", roles={"planner": "scout"}))
            .item
        )
        harness.source.items = [item]
        harness.outcomes = ["raise"]
        harness.loop.tick()
        first = harness.dispatched[0].assignment_json
        # The agent leaves the registry between attempts; the run keeps it.
        DbAgentRegistry(harness.config, harness.dstore, clock=harness.clock).archive(
            "scout", by="test"
        )
        harness.clock.t += 10_000
        harness.outcomes = ["completed"]
        harness.loop.tick()
        assert len(harness.dispatched) == 2
        assert harness.dispatched[1].assignment_json == first
        assert _assignment(harness.dispatched[1]).roles["planner"] == "scout"

    def test_a_planned_run_carries_what_its_agents_remember(self, tmp_path: Path) -> None:
        """S-A5 wired in: the loop's own memory service fills each binding's
        memory block, so a run really starts with what its agents know."""
        harness = Capturing(tmp_path)
        _agent(harness, "scout", ["planner"])
        _remember(harness, "scout", "The brief is due on Friday.", channel_id="chn_brief")
        item = (
            ControlService(harness.loop)
            .admit(
                CLIENT,
                WorkloadAdmission(
                    ask="Write the brief", roles={"planner": "scout"}, channel_id="chn_brief"
                ),
            )
            .item
        )
        harness.source.items = [item]
        harness.outcomes = ["completed"]
        harness.loop.tick()
        (dispatched,) = harness.dispatched
        planned = _assignment(dispatched)
        assert "The brief is due on Friday." in planned.agents["scout"].memory_block

    def test_a_run_in_another_channel_does_not_read_a_private_memory(self, tmp_path: Path) -> None:
        harness = Capturing(tmp_path)
        _agent(harness, "scout", ["planner"])
        _remember(harness, "scout", "The brief is due on Friday.", channel_id="chn_other")
        item = (
            ControlService(harness.loop)
            .admit(
                CLIENT,
                WorkloadAdmission(
                    ask="Write the brief", roles={"planner": "scout"}, channel_id="chn_brief"
                ),
            )
            .item
        )
        harness.source.items = [item]
        harness.outcomes = ["completed"]
        harness.loop.tick()
        (dispatched,) = harness.dispatched
        assert _assignment(dispatched).agents["scout"].memory_block == ""

    def test_a_polled_issue_runs_with_the_built_in_team(self, tmp_path: Path) -> None:
        harness = Capturing(tmp_path)
        harness.source.items = [gh_item("3")]
        harness.loop.tick()
        (dispatched,) = harness.dispatched
        planned = _assignment(dispatched)
        assert planned.is_default()
        assert dict(planned.roles) == BUILTIN_ROLES and planned.lead == ANGIE
        assert dispatched.lead_agent is None and dispatched.channel_id is None
        assert dispatched.chain_depth == 0

    def test_a_finished_issue_asked_for_again_is_planned_for_the_new_lead(
        self, tmp_path: Path
    ) -> None:
        harness = Capturing(tmp_path)
        _agent(harness, "chief", ["lead"])
        harness.source.items = [gh_item("7", repo="o/r")]
        harness.outcomes = ["blocked"]
        harness.loop.tick()
        assert _assignment(harness.dispatched[0]).lead == ANGIE

        def admit(repo: str, number: str, kind: Any) -> WorkItem:
            return gh_item(number, repo=repo, kind=kind)

        harness.source.admit = admit  # type: ignore[attr-defined]
        outcome = ControlService(harness.loop).admit(
            CLIENT, IssueAdmission(repository="o/r", number=7, lead="chief")
        )
        assert outcome.fresh
        harness.source.items = [outcome.item]
        harness.clock.t += 10_000
        harness.outcomes = ["completed"]
        harness.loop.tick()
        assert len(harness.dispatched) == 2
        planned = _assignment(harness.dispatched[1])
        assert planned.lead == "chief"
        assert dict(planned.roles) == BUILTIN_ROLES

    def test_a_finished_issue_relabelled_from_chat_takes_the_new_ask(self, tmp_path: Path) -> None:
        harness = Capturing(tmp_path)
        _agent(harness, "smith", ["builder"])
        harness.source.items = [gh_item("9", repo="o/r")]
        harness.outcomes = ["blocked"]
        harness.loop.tick()
        assert _assignment(harness.dispatched[0]).roles["builder"] == "builder"
        # The concierge labels the issue again for a person in a channel.
        harness.dstore.note_admission(
            "9", harness.clock(), repo="o/r", channel_id="chn_x", roles={"builder": "smith"}
        )
        harness.source.items = [gh_item("9", repo="o/r")]
        harness.clock.t += 10_000
        harness.outcomes = ["completed"]
        harness.loop.tick()
        assert len(harness.dispatched) == 2
        again = harness.dispatched[1]
        assert again.channel_id == "chn_x"
        planned = _assignment(again)
        assert dict(planned.roles) == {**BUILTIN_ROLES, "builder": "smith"}
        assert planned.channel_id == "chn_x"

    def test_a_finished_issue_relabelled_with_no_new_ask_keeps_its_plan(
        self, tmp_path: Path
    ) -> None:
        harness = Capturing(tmp_path)
        _agent(harness, "smith", ["builder"])

        def admit(repo: str, number: str, kind: Any) -> WorkItem:
            return gh_item(number, repo=repo, kind=kind)

        harness.source.admit = admit  # type: ignore[attr-defined]
        item = (
            ControlService(harness.loop)
            .admit(
                CLIENT,
                IssueAdmission(repository="o/r", number=4, roles={"builder": "smith"}),
            )
            .item
        )
        harness.source.items = [item]
        harness.outcomes = ["blocked"]
        harness.loop.tick()
        first = harness.dispatched[0].assignment_json
        harness.source.items = [gh_item("4", repo="o/r")]
        harness.clock.t += 10_000
        harness.outcomes = ["completed"]
        harness.loop.tick()
        assert len(harness.dispatched) == 2
        assert harness.dispatched[1].assignment_json == first

    def test_the_default_runner_hands_the_assignment_to_the_engine(self, tmp_path: Path) -> None:
        harness = Capturing(tmp_path)
        started: list[dict[str, Any]] = []

        class Engine:
            def start(self, outcome: str, **kwargs: Any) -> RunResult:
                started.append(kwargs)
                return RunResult(run_id=kwargs["run_id"], state="merged")

        class Handle:
            engine = Engine()

        harness.loop._live_run = lambda run_id: Handle()  # type: ignore[method-assign,assignment,return-value]
        planned = AgentAssignment(lead=ANGIE, roles={"planner": "planner"}, agents={})
        item = gh_item("5", assignment_json=planned.to_json())
        harness.loop._default_runner(item, harness.config, "r1", EventBus(), False)
        (kwargs,) = started
        assert kwargs["assignment"] == planned


class TestStore:
    def test_the_new_fields_round_trip(self, tmp_path: Path) -> None:
        harness = Capturing(tmp_path)
        item = WorkItem(
            item_id="api:k1",
            source_key="k1",
            title="t",
            kind="workload",
            channel_id="chn_1",
            lead_agent="chief",
            assignment_json='{"roles": {"planner": "scout"}}',
            origin_agent="scout",
            parent_item_id="api:k0",
            chain_depth=2,
        )
        assert harness.dstore.upsert_new(item, harness.clock())
        stored = harness.dstore.get("api:k1")
        assert stored is not None
        assert (
            stored.channel_id,
            stored.lead_agent,
            stored.assignment_json,
            stored.origin_agent,
            stored.parent_item_id,
            stored.chain_depth,
        ) == ("chn_1", "chief", '{"roles": {"planner": "scout"}}', "scout", "api:k0", 2)

    def test_a_note_from_chat_reaches_the_polled_issue(self, tmp_path: Path) -> None:
        harness = Capturing(tmp_path)
        harness.dstore.note_admission(
            "9",
            harness.clock(),
            repo="o/r",
            channel_id="chn_code",
            lead="concierge",
            roles={"builder": "smith"},
        )
        assert harness.dstore.upsert_new(gh_item("9", repo="o/r"), harness.clock())
        stored = harness.dstore.get("gh:9")
        assert stored is not None
        assert stored.channel_id == "chn_code" and stored.lead_agent == "concierge"
        assert stored.assignment_json is not None
        assert json.loads(stored.assignment_json)["roles"] == {"builder": "smith"}
        # Another repository's issue with the same number is not the one asked for.
        assert harness.dstore.upsert_new(gh_item("9", item_id="gh:o2/r:issue:9", repo="o2/r"), 1.0)
        other = harness.dstore.get("gh:o2/r:issue:9")
        assert other is not None and other.channel_id is None

    def test_a_consumed_note_is_not_replayed_on_a_later_ask(self, tmp_path: Path) -> None:
        """A chat request is spent by the item it fills: re-labelling the
        same issue months later runs it for whoever asks then, not for the
        channel and the agents of the old conversation."""
        harness = Capturing(tmp_path)
        harness.dstore.note_admission(
            "9",
            harness.clock(),
            repo="o/r",
            channel_id="chn_code",
            lead="concierge",
            roles={"builder": "smith"},
        )
        assert harness.dstore.upsert_new(gh_item("9", repo="o/r"), harness.clock())
        assert harness.dstore.get_value(_admission_key("9", "o/r")) is None
        # The issue is finished, then edited and labelled again much later:
        # new work, so a new row — and the old conversation's ask is gone,
        # rather than quietly steering a run nobody asked from chat.
        harness.dstore.mark_done("gh:9", harness.clock())
        harness.clock.t += 10_000
        assert harness.dstore.upsert_new(
            gh_item("9", repo="o/r", title="Do 9, differently"), harness.clock()
        )
        again = harness.dstore.get("gh:9")
        assert again is not None and again.title == "Do 9, differently"
        assert again.channel_id is None
        assert again.lead_agent is None and again.assignment_json is None


class TestMigration:
    def _run(self, path: Path, fn: Any, revision: str) -> None:
        engine = open_engine(path)
        try:
            with engine.begin() as conn:
                fn(alembic_config(conn), revision)
        finally:
            engine.dispose()

    def test_items_gain_the_columns_once(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        self._run(path, command.upgrade, "0026")
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO daemon_work_items (item_id, source_key, title, state, created_at,"
            " updated_at) VALUES ('gh:issue:1', '1', 'Old', 'done', 1, 1)"
        )
        conn.commit()
        conn.close()
        engine = open_engine(path)
        try:
            ensure_schema(engine)
        finally:
            engine.dispose()
        # Stamped back and upgraded again: the guarded columns are not re-added.
        self._run(path, command.stamp, "0026")
        engine = open_engine(path)
        try:
            ensure_schema(engine)
        finally:
            engine.dispose()
        conn = sqlite3.connect(path)
        try:
            columns = {row[1]: row for row in conn.execute("PRAGMA table_info(daemon_work_items)")}
            row = conn.execute(
                "SELECT channel_id, lead_agent, assignment_json, origin_agent,"
                " parent_item_id, chain_depth FROM daemon_work_items"
            ).fetchone()
        finally:
            conn.close()
        for name in ("channel_id", "lead_agent", "assignment_json", "origin_agent"):
            assert name in columns
        assert columns["chain_depth"][3] == 1  # NOT NULL
        assert row == (None, None, None, None, None, 0)
