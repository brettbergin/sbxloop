"""An agent starting work and filing issues on its own (S-A12).

The guardrails are the whole feature: an agent with `can_start` may put
work in the queue, and every one of the ways that could become a spiral --
a kind it was not given, a chain that keeps going, one agent spending the
whole day, the workspace budget, an unconfigured repository, the same ask
twice -- is refused by name, before anything is admitted or filed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sbxloop.agents.definition import AgentDefinition, AgentSpec
from sbxloop.agents.origin import WorkOrigin, origin_from_body, origin_marker
from sbxloop.agents.tools import agent_tool_handler, work_dedupe_key, work_granted
from sbxloop.config import Config
from sbxloop.daemon.agentwork import AgentWorkService
from sbxloop.daemon.controls.principal import ALL_CAPABILITIES, Principal
from sbxloop_worker.protocol import HostToolCall
from tests.unit.test_daemon_loop import Harness


class FakeIssueRef:
    def __init__(self, number: int, url: str) -> None:
        self.number = number
        self.url = url


class FakeGithub:
    """The daemon's github handle, narrowed to what filing needs."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str, str, list[str]]] = []
        self.fail: Exception | None = None

    def call(self, fn: Any) -> Any:
        return fn(self)

    def issue_create(
        self, repo: str, title: str, body: str, labels: list[str] | None = None
    ) -> FakeIssueRef:
        if self.fail is not None:
            raise self.fail
        self.created.append((repo, title, body, list(labels or [])))
        number = 100 + len(self.created)
        return FakeIssueRef(number, f"https://github.com/{repo}/issues/{number}")


def scout(**spec: Any) -> AgentDefinition:
    """An agent a person made that may start work."""
    fields: dict[str, Any] = {
        "slug": "scout",
        "name": "Scout",
        "roles": ["planner"],
        "can_start": ["workload"],
    }
    fields.update(spec)
    return AgentDefinition(spec=AgentSpec(**fields), source="user")


def service(tmp_path: Path, **sections: Any) -> tuple[AgentWorkService, Harness, FakeGithub]:
    config = Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "github": {"repo": "o/r"},
            "workloads": [{"name": "research", "sinks": ["chat"]}],
            **sections,
        }
    )
    harness = Harness(tmp_path, config)
    github = FakeGithub()
    harness.loop.github = github
    return AgentWorkService(harness.loop, clock=harness.clock), harness, github


def call(tools: list[Any], name: str, **arguments: Any) -> str:
    """Invoke one offered tool the way a session would, and return the text
    the agent reads (a refusal included)."""
    handler = agent_tool_handler(tools, None, agent_slug="scout")
    response = handler(HostToolCall(call_id="c1", name=name, arguments=arguments))
    return response.text or response.error or ""


def offered(service: AgentWorkService, agent: AgentDefinition, **kwargs: Any) -> list[Any]:
    return service.tools(agent, **kwargs)


class TestWhatIsOffered:
    def test_an_agent_with_no_can_start_is_offered_nothing(self, tmp_path: Path) -> None:
        work, _, _ = service(tmp_path)
        assert offered(work, scout(can_start=[])) == []
        assert not work_granted(scout(can_start=[]))

    def test_a_tools_list_narrows_the_work_tools_away(self, tmp_path: Path) -> None:
        work, _, _ = service(tmp_path)
        assert offered(work, scout(tools=["memory"])) == []
        assert offered(work, scout(tools=["start_run"])) != []

    def test_an_agent_that_may_start_work_gets_both_tools(self, tmp_path: Path) -> None:
        work, _, _ = service(tmp_path)
        names = {tool.spec.name for tool in offered(work, scout())}
        assert names == {"start_run", "file_issue"}


class TestStartRefusals:
    def test_a_kind_the_agent_does_not_declare_is_refused(self, tmp_path: Path) -> None:
        work, harness, _ = service(tmp_path)
        tools = offered(work, scout(can_start=["workload"]))
        text = call(tools, "start_run", kind="code", ask="rewrite everything")
        assert "code" in text and "workload" in text
        assert harness.dstore.items() == []

    def test_the_chain_depth_cap_stops_the_next_generation(self, tmp_path: Path) -> None:
        work, harness, _ = service(tmp_path, agent_team={"max_chain_depth": 2})
        # Depth 2 work may not start depth 3.
        tools = offered(work, scout(), parent_item_id="api:p", parent_depth=2)
        text = call(tools, "start_run", kind="workload", ask="go deeper")
        assert "max_chain_depth" in text
        assert harness.dstore.items() == []
        # One hop shallower still starts.
        tools = offered(work, scout(), parent_item_id="api:p", parent_depth=1)
        assert "queued workload" in call(tools, "start_run", kind="workload", ask="go deeper")

    def test_an_agent_may_not_start_more_than_its_daily_cap(self, tmp_path: Path) -> None:
        work, harness, _ = service(tmp_path, agent_team={"max_agent_runs_per_day": 1})
        tools = offered(work, scout())
        assert "queued workload" in call(tools, "start_run", kind="workload", ask="first")
        text = call(tools, "start_run", kind="workload", ask="second")
        assert "max_agent_runs_per_day" in text
        assert len(harness.dstore.items()) == 1

    def test_a_spec_cap_overrides_the_team_default(self, tmp_path: Path) -> None:
        work, harness, _ = service(tmp_path, agent_team={"max_agent_runs_per_day": 4})
        tools = offered(work, scout(max_runs_per_day=1))
        assert "queued workload" in call(tools, "start_run", kind="workload", ask="first")
        assert "max_runs_per_day" in call(tools, "start_run", kind="workload", ask="second")
        assert len(harness.dstore.items()) == 1

    def test_the_workspace_budget_refuses_before_anything_is_admitted(self, tmp_path: Path) -> None:
        from tests.unit.test_daemon_loop import gh_item

        work, harness, _ = service(tmp_path, daemon={"max_runs_per_day": 1})
        # The workspace has spent its one run for the day.
        harness.source.items = [gh_item("1")]
        assert harness.loop.tick().outcome == "done"
        queued = len(harness.dstore.items())
        tools = offered(work, scout())
        text = call(tools, "start_run", kind="workload", ask="spend")
        assert "run_cap" in text
        assert len(harness.dstore.items()) == queued

    def test_an_unconfigured_repository_is_refused(self, tmp_path: Path) -> None:
        work, harness, _ = service(tmp_path)
        tools = offered(work, scout(can_start=["code", "workload"]))
        text = call(tools, "start_run", kind="code", ask="fix it", repo="someone/else")
        assert "not configured" in text
        assert harness.dstore.items() == []

    def test_the_same_ask_twice_queues_one_run(self, tmp_path: Path) -> None:
        work, harness, _ = service(tmp_path)
        tools = offered(work, scout(), parent_item_id="api:p")
        assert "queued workload" in call(tools, "start_run", kind="workload", ask="Count  the  ")
        text = call(tools, "start_run", kind="workload", ask="count the")
        assert "already" in text
        assert len(harness.dstore.items()) == 1

    def test_a_blank_ask_is_refused(self, tmp_path: Path) -> None:
        work, harness, _ = service(tmp_path)
        text = call(offered(work, scout()), "start_run", kind="workload", ask="   ")
        assert "ask" in text
        assert harness.dstore.items() == []


class TestASuccessfulStart:
    def test_the_item_carries_the_channel_and_the_whole_chain(self, tmp_path: Path) -> None:
        work, harness, _ = service(tmp_path)
        tools = offered(
            work,
            scout(),
            channel_id="ch1",
            parent_item_id="api:parent",
            parent_depth=1,
            on_behalf_of="Brett",
        )
        text = call(
            tools, "start_run", kind="workload", ask="Summarise the week", profile="research"
        )
        assert "queued workload" in text
        (item,) = harness.dstore.items()
        assert item.kind == "workload" and item.profile == "research"
        assert item.channel_id == "ch1"
        assert item.origin_agent == "scout"
        assert item.parent_item_id == "api:parent"
        # The parent is depth 1, so the work it started is depth 2.
        assert item.chain_depth == 2
        # The ask a person reads says who asked and for whom.
        assert "Filed by the `scout` agent on behalf of Brett" in item.body

    def test_the_pool_is_asked_about_the_run_before_it_is_queued(self, tmp_path: Path) -> None:
        work, harness, _ = service(tmp_path, daemon={"daily_token_budget": 10})
        harness.dstore.record_usage(
            ts=harness.clock(),
            source="turn",
            ref_id="t1",
            agent_slug="scout",
            channel_id="ch1",
            input_tokens=8,
            output_tokens=8,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        tools = offered(work, scout(), channel_id="ch1")
        text = call(tools, "start_run", kind="workload", ask="spend more")
        assert "token_budget" in text
        assert harness.dstore.items() == []

    def test_the_admission_holds_items_create_and_nothing_else(self) -> None:
        principal = Principal.for_agent("scout", "Brett")
        assert principal.capabilities == frozenset({"items:create"})
        assert principal.kind == "agent"
        assert principal.capabilities < ALL_CAPABILITIES
        assert "scout" in (principal.attribution() or "")


class TestFileIssue:
    def test_an_unqueued_issue_carries_the_marker_and_the_footer(self, tmp_path: Path) -> None:
        work, _, github = service(tmp_path)
        tools = offered(work, scout(), parent_item_id="api:parent", parent_depth=0)
        text = call(tools, "file_issue", repo="o/r", title="Flaky test", body="It fails.")
        assert "NOT queued" in text
        (repo, title, body, labels) = github.created[0]
        assert (repo, title, labels) == ("o/r", "Flaky test", [])
        assert origin_from_body(body) == WorkOrigin("scout", "api:parent", 1)
        assert "Filed by the `scout` agent" in body

    def test_queueing_an_issue_needs_code_in_can_start(self, tmp_path: Path) -> None:
        work, _, github = service(tmp_path)
        tools = offered(work, scout(can_start=["workload"]))
        text = call(tools, "file_issue", repo="o/r", title="Do it", body="now", queue=True)
        assert "code" in text
        assert github.created == []

    def test_a_queued_issue_carries_the_trigger_label(self, tmp_path: Path) -> None:
        work, _, github = service(tmp_path)
        tools = offered(work, scout(can_start=["code", "workload"]))
        text = call(tools, "file_issue", repo="o/r", title="Do it", body="now", queue=True)
        assert "queued issue" in text
        (_, _, _, labels) = github.created[0]
        assert labels == [Config().labels_for().trigger]

    def test_a_code_start_files_a_queued_issue(self, tmp_path: Path) -> None:
        work, _, github = service(tmp_path)
        tools = offered(work, scout(can_start=["code"]), parent_item_id="api:p", parent_depth=1)
        text = call(tools, "start_run", kind="code", ask="Fix the flaky test\n\nDetails.")
        assert "queued issue" in text
        (repo, title, body, labels) = github.created[0]
        assert (repo, title) == ("o/r", "Fix the flaky test")
        assert labels == [Config().labels_for().trigger]
        assert origin_from_body(body) == WorkOrigin("scout", "api:p", 2)


class TestTheMarkerRoundTrip:
    def test_a_marker_renders_and_parses_back(self) -> None:
        origin = WorkOrigin("planner", "gh:issue:12", 2)
        assert origin_from_body(f"body\n{origin_marker(origin)}\n") == origin
        assert origin_from_body(origin_marker(WorkOrigin("planner"))) == WorkOrigin("planner")
        assert origin_from_body("no marker here") is None
        # The first marker wins: quoting an older issue cannot re-parent.
        two = f"{origin_marker(origin)}\n{origin_marker(WorkOrigin('critic', None, 0))}"
        assert origin_from_body(two) == origin

    def test_discovery_reads_the_marker_an_agent_left(self) -> None:
        from sbxloop.daemon.sources import GitHubIssueSource
        from tests.unit.test_daemon_sources import FIXTURE_NOW, LABELS, RecordingOps, issue

        origin = WorkOrigin("scout", "api:parent", 2)
        row = issue(7, "sbxloop:run")
        row["body"] = "Please. " + origin_marker(origin)
        source = GitHubIssueSource(
            lambda: RecordingOps({"7": row}),  # type: ignore[arg-type]
            "o/r",
            LABELS,
            host="db",
            clock=lambda: FIXTURE_NOW,
        )
        (item,) = source.poll()
        assert item.origin_agent == "scout"
        assert item.parent_item_id == "api:parent"
        assert item.chain_depth == 2

    def test_an_ordinary_issue_still_has_no_origin(self) -> None:
        from sbxloop.daemon.sources import GitHubIssueSource
        from tests.unit.test_daemon_sources import FIXTURE_NOW, LABELS, RecordingOps, issue

        source = GitHubIssueSource(
            lambda: RecordingOps({"8": issue(8, "sbxloop:run")}),  # type: ignore[arg-type]
            "o/r",
            LABELS,
            host="db",
            clock=lambda: FIXTURE_NOW,
        )
        (item,) = source.poll()
        assert item.origin_agent is None
        assert item.parent_item_id is None
        assert item.chain_depth == 0


def test_the_dedupe_key_folds_whitespace_and_case_but_not_the_agent() -> None:
    assert work_dedupe_key("scout", "api:p", "Count  the WORDS") == work_dedupe_key(
        "scout", "api:p", "count the words"
    )
    assert work_dedupe_key("scout", "api:p", "x") != work_dedupe_key("critic", "api:p", "x")
    assert work_dedupe_key("scout", "api:p", "x") != work_dedupe_key("scout", None, "x")


def test_agent_team_defaults_are_the_documented_ones() -> None:
    team = Config().agent_team
    assert (team.max_chain_depth, team.max_agent_runs_per_day) == (2, 4)
    with pytest.raises(ValueError, match="max_chain_depth"):
        Config.model_validate({"agent_team": {"max_chain_depth": -1}})
