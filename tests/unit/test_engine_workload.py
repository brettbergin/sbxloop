"""A ``workload`` run (#755, #756): the operator's stage list on the same
run shape — one sandbox, its own data directory, no repository — and the
two actors inside it: the operator that plans and executes, the judge that
holds each task to its acceptance criteria.

The engine harness drives a scripted agent through plan → (execute →
judge) per task → judgment → publish; every workload task consumes an
execute entry (text) and a judge entry (a verdict). The assertions are on
what the run leaves behind: the sandboxes it created, the states it
walked, the rows it persisted and what a resume makes of them. The
developer loop's own trail is gated separately (``test_code_run_trail.py``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sbxloop.engine.model import RESUMABLE_RUN_STATES, WORKLOAD_STAGES
from sbxloop.engine.phases import (
    AGENT_NAMES,
    JUDGE_SYSTEM_MESSAGE,
    OPERATOR_SYSTEM_MESSAGE,
    ToolDigest,
)
from sbxloop.errors import WorkerError
from sbxloop.events import HostEventTypes
from tests.conftest import FakeSbx
from tests.fakes.fake_github import FakeGithub
from tests.unit.test_engine import BUILD, FILES_BUILD, Harness, task, taskgraph

WORKLOAD_STATES = ["provisioning", "planning", "executing", "judging", "publishing", "completed"]

# The judge's scripted verdicts.
PASS = {"json": {"passed": True, "unmet": [], "notes": "every criterion met"}}


def fail(*unmet: str, notes: str = "") -> dict[str, Any]:
    return {"json": {"passed": False, "unmet": list(unmet), "notes": notes}}


def plan(*tasks: dict[str, Any], title: str | None = None) -> dict[str, Any]:
    return {"json": {"title": title, "tasks": list(tasks)}}


def jobs(harness: Harness, run_id: str, phase: str) -> list[dict[str, Any]]:
    """The agent-session jobs of one workload phase (``plan``, ``execute``,
    ``judge``), told apart by the actor's system prompt and the reply
    shape — job files carry no phase name and no order (keep_sandboxes
    runs only)."""
    sessions = [j for j in harness.agent_jobs(run_id) if j["kind"] == "agent.session"]
    if phase == "judge":
        return [j for j in sessions if j["system_message"] == JUDGE_SYSTEM_MESSAGE]
    expect = "json" if phase == "plan" else "text"
    return [
        j
        for j in sessions
        if j["system_message"] == OPERATOR_SYSTEM_MESSAGE and j["expect"] == expect
    ]


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


class TestWorkloadRun:
    def test_one_sandbox_and_the_operator_stages(self, harness: Harness) -> None:
        """No `[github]`: the agent box alone, the four stages in order,
        `completed` at the end — and the run says what it is."""
        harness.script([taskgraph(task("t1", verify=["test -f hello.txt"])), FILES_BUILD, PASS])
        engine = harness.engine(keep_sandboxes=True)
        result = engine.start("write hello.txt", kind="workload")

        assert result.state == "completed" and result.kind == "workload"
        assert result.succeeded
        assert harness.run_states() == WORKLOAD_STATES
        assert harness.sandboxes_left() == [f"sbxloop-{result.run_id}-agent"]
        run = engine.store.get_run(result.run_id)
        assert run.kind == "workload" and run.state == "completed"
        assert run.stage == "publishing", "the last non-terminal stage, for a resume"
        # The data directory: the per-run dir an unconfigured code run gets,
        # harvested rather than mounted as the work.
        assert result.workspace == harness.state_dir / "runs" / result.run_id / "workspace"
        (start,) = [e for e in harness.events if e.type == HostEventTypes.RUN_START]
        assert start.data["kind"] == "workload"
        assert start.data["workspace"] is None
        assert start.data["workspace_source"] == "data-dir"
        # The toolchain set is the config's answer, not a detection over an
        # empty directory.
        (languages,) = [e for e in harness.events if e.type == "sandbox.languages"]
        assert languages.data["source"] == "default"
        # The operator planned and executed, the judge passed the task, and
        # the judgment re-ran the task's check on the finished workspace.
        rows = [
            (r["phase"], r["task_id"], r["status"])
            for r in engine.store.phase_attempts(result.run_id)
        ]
        assert rows == [
            ("plan", None, "ok"),
            ("execute", "t1", "ok"),
            ("judge", "t1", "ok"),
            ("judge", None, "ok"),
        ]
        ends = [
            (e.data["phase"], e.data["status"])
            for e in harness.events
            if e.type == HostEventTypes.PHASE_END and e.data.get("task_id") is None
        ]
        assert ends == [("judge", "ok")], "no gate, no review: a workload's own stages only"

    def test_a_configured_repository_changes_nothing(self, harness: Harness) -> None:
        """`[github]` set but the run is a workload: still one sandbox, no
        delivery, and GitHub is never called."""
        fake = FakeGithub()
        harness.script([taskgraph(task("t1")), BUILD, PASS])
        engine = harness.engine(ops=fake, github={"repo": fake.repo}, keep_sandboxes=True)
        result = engine.start("count the things", kind="workload")

        assert result.state == "completed"
        assert harness.run_states() == WORKLOAD_STATES
        assert harness.sandboxes_left() == [f"sbxloop-{result.run_id}-agent"]
        assert result.pr_number is None and fake.pr_create_calls == 0
        assert not any(e.type.startswith("run.deliver") for e in harness.events)
        assert not any(e.type.startswith("sandbox.workspace_clone") for e in harness.events)

    def test_the_workspace_config_belongs_to_code_runs(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        """A configured `[sandbox] workspace` is not mounted into a workload:
        the run works in its data directory whatever the config names."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "README.md").write_text("theirs\n")
        harness.script([taskgraph(task("t1")), BUILD, PASS])
        engine = harness.engine(
            sandbox={"workspace": str(checkout), "workspace_isolation": "in-place"}
        )
        result = engine.start("count the things", kind="workload")
        assert result.state == "completed"
        assert result.workspace == harness.state_dir / "runs" / result.run_id / "workspace"
        assert not (result.workspace / "README.md").exists()

    def test_setup_commands_are_a_checkouts(self, harness: Harness) -> None:
        """`setup_commands` prepare a clone; a workload has none to prepare."""
        harness.script([taskgraph(task("t1")), BUILD, PASS])
        engine = harness.engine(sandbox={"setup_commands": ["exit 9"]})
        result = engine.start("count the things", kind="workload")
        assert result.state == "completed"
        assert not any(e.type == HostEventTypes.SANDBOX_SETUP for e in harness.events)

    def test_a_red_judgment_ends_the_run_named(self, harness: Harness) -> None:
        """A later task undid what an earlier one proved: the judge catches
        it on the finished workspace and the run fails naming the check."""
        gate = "grep -q green state.txt"
        harness.script(
            [
                taskgraph(task("t1", verify=[gate]), task("t2", deps=["t1"])),
                {"text": "t1", "files": {"state.txt": "green\n"}},
                PASS,
                {"text": "t2 broke it", "files": {"state.txt": "red\n"}},
                PASS,
            ]
        )
        engine = harness.engine()
        result = engine.start("keep it green", kind="workload")

        assert result.state == "failed"
        assert result.reason is not None
        # t2's own `true` is the other check: the count is over every task's.
        assert result.reason == f"the judgment failed: 1 of 2 check(s) red — `{gate}` (exit 1)"
        assert harness.run_states() == [
            "provisioning",
            "planning",
            "executing",
            "judging",
            "failed",
        ]
        assert [(t.spec.id, t.state) for t in result.tasks] == [("t1", "done"), ("t2", "done")]
        (judge,) = [
            r
            for r in engine.store.phase_attempts(result.run_id)
            if r["phase"] == "judge" and r["task_id"] is None
        ]
        assert judge["status"] == "failed"
        run = engine.store.get_run(result.run_id)
        assert run.state == "failed" and run.stage == "judging"

    def test_a_failed_task_fails_the_run_before_the_judgment(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), BUILD, fail("t1 works — nothing was written")])
        engine = harness.engine(budgets={"max_revisions_per_task": 0, "max_replans_per_task": 0})
        result = engine.start("write never.txt", kind="workload")
        assert result.state == "failed"
        assert "judging" not in harness.run_states()
        rows = engine.store.phase_attempts(result.run_id)
        assert not any(r["phase"] == "judge" and r["task_id"] is None for r in rows)


class TestOperatorAndJudge:
    """#756: the operator's plan and reports, the judge's verdicts, and
    what a failing verdict does to the task."""

    def test_the_plan_names_the_run_and_declares_needs(self, harness: Harness) -> None:
        spec = task("t1")
        spec["needs"] = {
            "hosts": ["Api.Example.COM"],
            "credentials": [],
            "sink": "chat",
            "repo": None,
        }
        harness.script([plan(spec, title="Count the widgets"), BUILD, PASS])
        # A profile that grants the needs (#758); without one they are refused.
        engine = harness.engine(
            keep_sandboxes=True,
            workloads=[{"name": "api", "egress": ["*.example.com"], "sinks": ["chat"]}],
            workload={"default": "api"},
        )
        result = engine.start("count the widgets from the api", kind="workload")

        assert result.state == "completed"
        assert engine.store.get_run(result.run_id).pr_title == "Count the widgets"
        (t1,) = engine.store.get_tasks(result.run_id)
        assert t1.spec.needs.hosts == ["api.example.com"]
        assert t1.spec.needs.credentials == []
        assert t1.spec.needs.sink == "chat" and t1.spec.needs.repo is None
        (row,) = [r for r in engine.store.phase_attempts(result.run_id) if r["phase"] == "plan"]
        assert row["task_id"] is None and "Count the widgets" in row["output_json"]
        # The operator's prompt carries the needs by name.
        (execute,) = jobs(harness, result.run_id, "execute")
        assert "api.example.com" in execute["prompt"] and "sink: chat" in execute["prompt"]

    def test_the_judge_passes_a_task_it_read(self, harness: Harness) -> None:
        """The scripted run of the issue: plan → two tasks → execute → judge
        passes → completed, with `judge.verdict passed=true` on each."""
        harness.script(
            [
                taskgraph(task("t1"), task("t2", deps=["t1"])),
                {"text": "t1 done"},
                PASS,
                {"text": "t2 done"},
                PASS,
            ]
        )
        engine = harness.engine(keep_sandboxes=True)
        result = engine.start("two things", kind="workload")

        assert result.state == "completed"
        assert [(t.spec.id, t.state, t.revisions) for t in result.tasks] == [
            ("t1", "done", 0),
            ("t2", "done", 0),
        ]
        verdicts = [e for e in harness.events if e.type == HostEventTypes.JUDGE_VERDICT]
        assert [(e.data["task_id"], e.data["attempt"], e.data["passed"]) for e in verdicts] == [
            ("t1", 1, True),
            ("t2", 1, True),
        ]
        assert all(e.data["unmet"] == [] for e in verdicts)
        # Two actors, neither the developer loop's agent: the operator plans
        # and executes, the judge judges — read-only, and as itself rather
        # than a coding agent with a note appended.
        assert AGENT_NAMES["operator_plan"] == AGENT_NAMES["operator_execute"] == "operator"
        assert AGENT_NAMES["operator_judge"] == "judge"
        plans = jobs(harness, result.run_id, "plan")
        executes = jobs(harness, result.run_id, "execute")
        judges = jobs(harness, result.run_id, "judge")
        assert (len(plans), len(executes), len(judges)) == (1, 2, 2)
        assert all(j["permission_mode"] == "read_only" for j in judges)
        assert all(j["expect"] == "json" for j in judges)
        assert all(j["system_preset"] is False for j in plans + executes + judges)
        # The judge's job carries no host tools: it reads and never calls out.
        assert all(j["host_tools"] == [] for j in plans + judges)

    def test_a_failing_verdict_is_the_next_attempts_feedback(self, harness: Harness) -> None:
        """Judge fails once with unmet → re-executed with that feedback →
        passes; `revisions == 1`."""
        harness.script(
            [
                taskgraph(task("t1")),
                {"text": "wrote nothing"},
                fail("t1 works — no output file was written", notes="the record shows no writes"),
                {"text": "wrote it this time"},
                PASS,
            ]
        )
        engine = harness.engine(keep_sandboxes=True)
        result = engine.start("one thing", kind="workload")

        assert result.state == "completed"
        (t1,) = result.tasks
        assert t1.state == "done" and t1.revisions == 1
        verdicts = [
            (e.data["attempt"], e.data["passed"], e.data["unmet"])
            for e in harness.events
            if e.type == HostEventTypes.JUDGE_VERDICT
        ]
        assert verdicts == [
            (1, False, ["t1 works — no output file was written"]),
            (2, True, []),
        ]
        executes = jobs(harness, result.run_id, "execute")
        (second,) = [j for j in executes if "no output file was written" in j["prompt"]]
        (first,) = [j for j in executes if j is not second]
        assert "the judge found these acceptance criteria unmet" not in first["prompt"]
        assert "the judge found these acceptance criteria unmet" in second["prompt"]
        assert "- t1 works — no output file was written" in second["prompt"]
        assert "the record shows no writes" in second["prompt"]
        # The second attempt sees the first attempt's own report.
        assert "wrote nothing" in second["prompt"]
        rows = [
            (r["phase"], r["attempt"], r["status"])
            for r in engine.store.phase_attempts(result.run_id)
            if r["task_id"] == "t1"
        ]
        assert rows == [
            ("execute", 1, "ok"),
            ("judge", 1, "failed"),
            ("execute", 2, "ok"),
            ("judge", 2, "ok"),
        ]
        ends = [
            (e.data["phase"], e.data["status"], e.data["message"])
            for e in harness.events
            if e.type == HostEventTypes.PHASE_END and e.data.get("task_id") == "t1"
        ]
        assert ends[1] == ("judge", "failed", "unmet: t1 works — no output file was written")
        assert ends[3] == ("judge", "ok", "every criterion met")

    def test_failing_past_the_budget_fails_the_run_naming_the_criterion(
        self, harness: Harness
    ) -> None:
        harness.script(
            [
                taskgraph(task("t1")),
                BUILD,
                fail("t1 works — the count is missing"),
                BUILD,
                fail("t1 works — the count is missing", "t1 works — still no file"),
            ]
        )
        engine = harness.engine(budgets={"max_revisions_per_task": 1, "max_replans_per_task": 3})
        result = engine.start("one thing", kind="workload")

        assert result.state == "failed"
        (t1,) = result.tasks
        assert t1.state == "failed" and t1.revisions == 2
        assert result.reason == (
            "task t1 failed the judgment after 2 attempt(s) — "
            "unmet: t1 works — the count is missing (+1 more)"
        )
        # No replan: the judge's criteria are the plan's, and a verify
        # command that never changes is the code loop's diagnosis, not ours.
        assert t1.replans == 0
        assert "judging" not in harness.run_states()

    def test_two_malformed_verdicts_fail_the_task_closed(self, harness: Harness) -> None:
        """The judge answers twice with no verdict in it: `judge.degraded`,
        the task fails, the run fails — silence is never a pass."""
        harness.script(
            [
                taskgraph(task("t1")),
                BUILD,
                {"text": "looks fine to me"},
                {"json": {"passed": "maybe"}},
            ]
        )
        engine = harness.engine()
        result = engine.start("one thing", kind="workload")

        assert result.state == "failed"
        (t1,) = result.tasks
        assert t1.state == "failed" and t1.revisions == 0
        assert not any(e.type == HostEventTypes.JUDGE_VERDICT for e in harness.events)
        (degraded,) = [e for e in harness.events if e.type == HostEventTypes.JUDGE_DEGRADED]
        assert degraded.data["task_id"] == "t1" and degraded.data["attempt"] == 1
        assert "operator_judge produced invalid output twice" in degraded.data["error"]
        assert result.reason is not None
        assert result.reason.startswith("the judge could not reach a verdict on task t1")
        assert result.reason.endswith("; the run fails closed")
        (row,) = [r for r in engine.store.phase_attempts(result.run_id) if r["phase"] == "judge"]
        assert row["status"] == "failed" and '"degraded": true' in row["output_json"]

    def test_a_failing_verdict_must_name_a_criterion(self, harness: Harness) -> None:
        """`passed: false` with an empty `unmet` is a malformed verdict: it
        is sent back once, and a proper one on the retry counts."""
        harness.script(
            [
                taskgraph(task("t1")),
                BUILD,
                {"json": {"passed": False, "unmet": [], "notes": "meh"}},
                PASS,
            ]
        )
        result = harness.engine(keep_sandboxes=True).start("one thing", kind="workload")
        assert result.state == "completed"
        judges = jobs(harness, result.run_id, "judge")
        assert len(judges) == 2
        assert sum("Previous attempt was invalid" in j["prompt"] for j in judges) == 1
        assert not any(e.type == HostEventTypes.JUDGE_DEGRADED for e in harness.events)

    def test_the_judge_reads_the_tool_record_and_the_evidence(self, harness: Harness) -> None:
        """What the operator's session did reaches the judge as a digest,
        persisted on the execute row for a resume, and the task's declared
        checks run before the verdict as mechanical evidence."""
        harness.script(
            [
                taskgraph(task("t1", verify=["test -f hello.txt"])),
                {
                    "text": "wrote hello.txt",
                    "files": {"hello.txt": "hi\n"},
                    "events": [
                        {
                            "type": "agent.tool_end",
                            "data": {
                                "tool": "Bash",
                                "args": {"command": "echo hi > hello.txt"},
                                "success": True,
                            },
                        },
                        {
                            "type": "agent.tool_end",
                            "data": {
                                "tool": "Read",
                                "args": {"path": "hello.txt"},
                                "success": False,
                            },
                        },
                    ],
                },
                PASS,
            ]
        )
        engine = harness.engine(keep_sandboxes=True)
        result = engine.start("write hello.txt", kind="workload")
        assert result.state == "completed"

        (judge_job,) = jobs(harness, result.run_id, "judge")
        prompt = judge_job["prompt"]
        assert "`Bash`" in prompt and "echo hi > hello.txt" in prompt
        assert "`Read`" in prompt and "— failed" in prompt
        assert "wrote hello.txt" in prompt, "the report is the judge's claim to check"
        assert "test -f hello.txt" in prompt and "exit 0" in prompt
        (execute,) = [
            r for r in engine.store.phase_attempts(result.run_id) if r["phase"] == "execute"
        ]
        assert '"tool_calls": 2' in execute["output_json"]
        assert "echo hi > hello.txt" in execute["output_json"]
        # The relayed tool events carry the operator's name, not the builder's.
        tools = [e for e in harness.events if e.type == "agent.tool_end"]
        assert [e.data.get("agent") for e in tools] == ["operator", "operator"]


class TestTaskOutputs:
    """What a task leaves behind is persisted with it (#757): the report's
    result section and the data-directory files its attempts touched."""

    def test_each_task_persists_its_output(self, harness: Harness) -> None:
        harness.script(
            [
                taskgraph(task("t1"), task("t2", deps=["t1"])),
                {
                    "text": "Set up, then wrote.\n\n## Result\n\nwrote `hello.txt` with one line\n",
                    "files": {"hello.txt": "hi\n"},
                },
                PASS,
                {"text": "## Result\nread hello.txt; nothing written"},
                PASS,
            ]
        )
        engine = harness.engine()
        result = engine.start("two things", kind="workload")

        assert result.state == "completed"
        t1, t2 = result.tasks
        assert t1.output is not None and t2.output is not None
        assert t1.output.summary == "wrote `hello.txt` with one line"
        assert t1.output.text == "wrote `hello.txt` with one line"
        assert t1.output.files == ["hello.txt"] and t1.output.more_files == 0
        # t2 touched nothing: a file an earlier task wrote is not its output.
        assert (t2.output.summary, t2.output.files) == ("read hello.txt; nothing written", [])
        outputs = [
            (e.data["task_id"], e.data["attempt"], e.data["summary"], e.data["files"])
            for e in harness.events
            if e.type == HostEventTypes.TASK_OUTPUT
        ]
        assert outputs == [
            ("t1", 1, "wrote `hello.txt` with one line", 1),
            ("t2", 1, "read hello.txt; nothing written", 0),
        ]
        # Read back from the store, as a resume or `status` would.
        stored = harness.engine().store.get_tasks(result.run_id)
        assert [t.output for t in stored] == [t1.output, t2.output]
        assert [tid for tid, _ in result.outputs] == ["t1", "t2"]
        assert result.summary == (
            "2/2 task(s) passed the judge\n"
            "t1: wrote `hello.txt` with one line (1 file)\n"
            "t2: read hello.txt; nothing written"
        )

    def test_a_revision_keeps_the_earlier_attempts_files(self, harness: Harness) -> None:
        """The marker is set once, before the first attempt: the output of
        a revised task lists what every attempt left, under the latest
        report."""
        harness.script(
            [
                taskgraph(task("t1")),
                {"text": "## Result\nwrote a", "files": {"a.txt": "a\n"}},
                fail("t1 works — b is missing"),
                {"text": "## Result\nwrote b as well", "files": {"b.txt": "b\n"}},
                PASS,
            ]
        )
        result = harness.engine().start("one thing", kind="workload")
        assert result.state == "completed"
        (t1,) = result.tasks
        assert t1.output is not None
        assert t1.output.files == ["a.txt", "b.txt"]
        assert t1.output.summary == "wrote b as well"

    def test_a_resumed_run_keeps_the_files_an_earlier_sandbox_listed(
        self, harness: Harness
    ) -> None:
        """A resume boots a fresh sandbox with a fresh marker; the earlier
        attempt's files come from the persisted output, and only the ones
        still in the data directory."""
        harness.script(
            [
                taskgraph(task("t1")),
                {
                    "text": "## Result\nwrote a and gone",
                    "files": {"a.txt": "a\n", "gone.txt": "x\n"},
                },
                fail("t1 works — b is missing"),
                {"fail": "sandbox exploded"},
            ]
        )
        engine = harness.engine()
        with pytest.raises(WorkerError, match="sandbox exploded"):
            engine.start("one thing", kind="workload")
        (run,) = engine.store.list_runs()
        (t1,) = engine.store.get_tasks(run.run_id)
        assert t1.output is not None and t1.output.files == ["a.txt", "gone.txt"]
        (run.workspace / "gone.txt").unlink()  # type: ignore[union-attr]
        harness.script([{"text": "## Result\nwrote b", "files": {"b.txt": "b\n"}}, PASS])
        result = harness.engine().resume(run.run_id)
        assert result.state == "completed"
        (t1,) = result.tasks
        assert t1.output is not None
        assert t1.output.files == ["a.txt", "b.txt"]

    def test_excluded_trees_are_not_outputs(self, harness: Harness) -> None:
        """The harvest's excludes apply to the listing: a task's `.git` or
        dependency tree is never its result."""
        harness.script(
            [
                taskgraph(task("t1")),
                {
                    "text": "## Result\nbuilt it",
                    "files": {
                        "out/report.md": "# r\n",
                        ".git/config": "[core]\n",
                        "node_modules/x/index.js": "0\n",
                        "__pycache__/m.pyc": "0",
                    },
                },
                PASS,
            ]
        )
        result = harness.engine().start("one thing", kind="workload")
        (t1,) = result.tasks
        assert t1.output is not None
        assert t1.output.files == ["out/report.md"]

    def test_a_report_without_a_result_section_is_the_output_whole(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), {"text": "  did the thing\nand more  "}, PASS])
        result = harness.engine().start("one thing", kind="workload")
        (t1,) = result.tasks
        assert t1.output is not None
        assert (t1.output.summary, t1.output.text) == ("did the thing", "did the thing\nand more")

    def test_a_code_run_has_no_outputs(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), FILES_BUILD])
        result = harness.engine().start("one thing")
        assert [t.output for t in result.tasks] == [None]
        assert result.summary is None and result.outputs == []
        assert not [e for e in harness.events if e.type == HostEventTypes.TASK_OUTPUT]


def _event(type: str, **data: Any) -> Any:
    from sbxloop_worker.protocol import Event

    return Event(ts=0.0, run_id="r1", type=type, data=data)


class TestToolDigest:
    def test_records_ends_only_and_clips(self) -> None:
        digest = ToolDigest()
        digest.record(_event("agent.tool_start", tool="Bash", args="x"))
        digest.record(_event("agent.tool_end", tool="Bash", args="a" * 500, success=True))
        digest.record(_event("agent.tool_end", tool="Write", args="f"))
        assert digest.total == 2
        text = digest.render()
        assert text.splitlines()[0].startswith("1. `Bash` ")
        assert "— ok" in text and "— ?" in text
        assert "a" * 500 not in text

    def test_empty_and_overflow(self) -> None:
        digest = ToolDigest()
        assert digest.render() == "(no tool calls were made)"
        for i in range(90):
            digest.record(_event("agent.tool_end", tool=f"t{i}", success=True))
        assert digest.total == 90
        assert digest.render().endswith("… and 10 more call(s)")


class TestWorkloadResume:
    def _interrupted_in_executing(self, harness: Harness) -> tuple[Harness, str]:
        harness.script([taskgraph(task("t1")), {"fail": "sandbox exploded"}])
        engine = harness.engine()
        with pytest.raises(WorkerError, match="sandbox exploded"):
            engine.start("count the things", kind="workload")
        (run,) = engine.store.list_runs()
        assert run.kind == "workload"
        assert run.state == "executing" and run.stage == "executing"
        assert [t.state for t in engine.store.get_tasks(run.run_id)] == ["executing"]
        return harness, run.run_id

    def test_resume_re_enters_executing_and_keeps_the_kind(self, harness: Harness) -> None:
        """The kind is the row's, never the config's: a fresh engine under
        a config that says nothing of workloads resumes the workload."""
        harness, run_id = self._interrupted_in_executing(harness)
        harness.events.clear()
        harness.script([BUILD, PASS])
        result = harness.engine(github={"repo": "o/r"}).resume(run_id)

        assert result.state == "completed" and result.kind == "workload"
        assert harness.run_states() == [
            "provisioning",
            "executing",
            "judging",
            "publishing",
            "completed",
        ]
        (start,) = [e for e in harness.events if e.type == HostEventTypes.RUN_START]
        assert start.data["resumed"] is True and start.data["kind"] == "workload"
        # Still one sandbox, still the same data directory.
        assert harness.sandboxes_left() == []
        assert result.workspace == harness.state_dir / "runs" / run_id / "workspace"
        phases = [r["phase"] for r in harness.engine().store.phase_attempts(run_id)]
        assert phases.count("plan") == 1 and phases.count("execute") == 1
        assert "decompose" not in phases and "build" not in phases

    def test_resume_at_judging_re_judges(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1", verify=["test -f hello.txt"])), FILES_BUILD, PASS])
        engine = harness.engine()
        result = engine.start("write hello.txt", kind="workload")
        assert result.state == "completed"
        # Park the run as if it had died in the judgment.
        engine.store.set_run_state(result.run_id, "judging")
        harness.events.clear()
        harness.script([])
        resumed = harness.engine().resume(result.run_id)
        assert resumed.state == "completed"
        assert harness.run_states() == ["provisioning", "judging", "publishing", "completed"]
        judged = [
            r
            for r in engine.store.phase_attempts(result.run_id)
            if r["phase"] == "judge" and r["task_id"] is None
        ]
        assert [r["attempt"] for r in judged] == [1, 2]

    def test_resume_at_verifying_re_judges_the_persisted_report(self, harness: Harness) -> None:
        """Died between execute and judge: the judge reads the execute row
        (report and tool record), no re-execution."""
        harness.script([taskgraph(task("t1")), {"text": "t1 report"}, PASS])
        engine = harness.engine()
        result = engine.start("one thing", kind="workload")
        assert result.state == "completed"
        engine.store.set_run_state(result.run_id, "executing")
        (t1,) = engine.store.get_tasks(result.run_id)
        t1.state = "verifying"
        engine.store.update_task(result.run_id, t1)
        harness.events.clear()
        harness.script([fail("t1 works — not on the second look"), {"text": "again"}, PASS])
        resumed = harness.engine(keep_sandboxes=True).resume(result.run_id)
        assert resumed.state == "completed"
        (t1,) = resumed.tasks
        assert t1.revisions == 1
        ends = [
            (e.data["phase"], e.data["status"])
            for e in harness.events
            if e.type == HostEventTypes.PHASE_END and e.data.get("task_id") == "t1"
        ]
        assert ends == [("judge", "failed"), ("execute", "ok"), ("judge", "ok")]
        # The judge on resume read the persisted report, not a fresh one.
        judged_reports = [
            j for j in jobs(harness, resumed.run_id, "judge") if "t1 report" in j["prompt"]
        ]
        assert judged_reports

    def test_every_workload_stage_is_resumable(self) -> None:
        assert set(WORKLOAD_STAGES) <= RESUMABLE_RUN_STATES


# -- a workload's needs against its profile (#758) ---------------------------

WEATHER = {
    "name": "weather",
    "env": "WEATHER_API_KEY",
    "host": "api.weather.example.com",
    "description": "forecasts",
}
RESEARCH = {
    "name": "research",
    "description": "reads the web",
    "egress": ["*.example.com"],
    "credentials": ["weather"],
    "sinks": ["chat"],
    "repo": False,
    "budgets": {"max_tasks": 4},
}
CALL = {
    "name": "call_service",
    "arguments": {"credential": "weather", "method": "GET", "path": "/v1/forecast"},
    "call_id": "c1",
}


def needing(id: str, **needs: Any) -> dict[str, Any]:
    """A task that declares ``needs`` (hosts, credentials, sink, repo)."""
    spec = task(id)
    spec["needs"] = needs
    return spec


@pytest.fixture
def profiled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Config overrides for a run under the `research` profile, with the
    credential's value in the daemon's env and a scripted service."""
    from sbxloop_worker.serviceops import FAKE_ENV

    script = tmp_path / "service.json"
    script.write_text(json.dumps({"responses": [{"status": 200, "body": {"temp": 3}}]}))
    monkeypatch.setenv(FAKE_ENV, str(script))
    monkeypatch.setenv("WEATHER_API_KEY", "wx-secret-value-9f8e7d")
    return {
        "credentials": [WEATHER],
        "workloads": [RESEARCH],
        "workload": {"default": "research"},
    }


class TestNeeds:
    def refused(self, harness: Harness) -> list[dict[str, Any]]:
        return [e.data for e in harness.events if e.type == HostEventTypes.RUN_NEEDS_REFUSED]

    def granted(self, harness: Harness) -> list[dict[str, Any]]:
        return [e.data for e in harness.events if e.type == HostEventTypes.RUN_NEEDS_GRANTED]

    def assert_no_secret(self, harness: Harness) -> None:
        for event in harness.events:
            assert "wx-secret-value" not in json.dumps(event.data, default=str), event.type

    def test_a_host_inside_the_profile_is_granted_at_execute(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        """The profile's egress widens what a plan may declare: the host is
        allowed on the agent box at the task's execute entry, and the run
        says what it granted."""
        harness.script([plan(needing("t1", hosts=["data.example.com"])), BUILD, PASS])
        result = harness.engine(**profiled).start("read the data", kind="workload")
        assert result.state == "completed"
        agent = f"sbxloop-{result.run_id}-agent"
        assert ["allow", "network", "data.example.com", "--sandbox", agent] in (
            harness.fake_sbx.policies()
        )
        (allowed,) = [e for e in harness.events if e.type == "policy.allow"]
        assert allowed.data["domain"] == "data.example.com"
        assert allowed.data["reason"] == "declared in the plan's needs"
        assert allowed.data["task_id"] == "t1"
        (grant,) = self.granted(harness)
        assert grant["profile"] == "research"
        assert grant["hosts"] == ["data.example.com"]
        assert grant["credentials"] == [] and grant["repos"] == []
        assert grant["message"] == "granted under profile 'research': hosts `data.example.com`"
        assert self.refused(harness) == []
        # the profile's budgets rode along into the run's config
        stored = json.loads(harness.engine().store.get_run_config(result.run_id))
        assert stored["budgets"]["max_tasks"] == 4

    def test_a_host_outside_the_profile_fails_the_run_before_any_task(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        harness.script([plan(needing("t1", hosts=["other.example.org"]), task("t2")), BUILD, PASS])
        engine = harness.engine(**profiled)
        result = engine.start("read elsewhere", kind="workload")
        assert result.state == "failed"
        assert result.reason == (
            "the plan's needs were refused: task t1 needs host `other.example.org` — outside "
            "profile 'research'; `workloads.research.egress` in sbxloop.toml would allow it"
        )
        (refusal,) = self.refused(harness)
        assert refusal["key"] == "workloads.research.egress"
        assert (refusal["need"], refusal["value"], refusal["task_id"]) == (
            "host",
            "other.example.org",
            "t1",
        )
        assert self.granted(harness) == []
        assert [e for e in harness.events if e.type == "policy.allow"] == []
        assert harness.run_states() == ["provisioning", "planning", "failed"]
        assert all(t.state == "pending" for t in engine.store.get_tasks(result.run_id))
        phases = [r["phase"] for r in engine.store.phase_attempts(result.run_id)]
        assert phases == ["plan"], "no task ran"

    def test_a_denied_host_is_refused_even_inside_the_profile(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        harness.script([plan(needing("t1", hosts=["bad.example.com"])), BUILD, PASS])
        result = harness.engine(**profiled, policy={"deny": ["bad.example.com"]}).start(
            "reach a denied host", kind="workload"
        )
        assert result.state == "failed"
        (refusal,) = self.refused(harness)
        assert refusal["key"] is None
        assert "deny" in refusal["message"]

    def test_every_need_is_answered_before_the_run_fails(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        """All refusals are on the record at once, and the reason counts
        the rest — the operator fixes the config in one round."""
        harness.script(
            [
                plan(
                    needing("t1", hosts=["a.example.org"], credentials=["mail"]),
                    needing("t2", sink="issue", repo="o/r"),
                )
            ]
        )
        result = harness.engine(**profiled).start("ask for everything", kind="workload")
        assert result.state == "failed"
        assert (result.reason or "").endswith("(+3 more)")
        refusals = self.refused(harness)
        assert [(r["need"], r["key"]) for r in refusals] == [
            ("host", "workloads.research.egress"),
            ("credential", "credentials"),
            ("sink", "workloads.research.sinks"),
            ("repo", "workloads.research.repo"),
        ]
        assert "not in the [[credentials]] catalogue" in refusals[1]["message"]

    def test_a_catalogued_credential_the_profile_lacks_names_the_profile_key(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        other = {"name": "mail", "env": "MAIL_TOKEN", "host": "mail.example.com"}
        harness.script([plan(needing("t1", credentials=["mail"]))])
        result = harness.engine(**{**profiled, "credentials": [WEATHER, other]}).start(
            "send mail", kind="workload"
        )
        assert result.state == "failed"
        (refusal,) = self.refused(harness)
        assert refusal["key"] == "workloads.research.credentials"
        assert "not granted by profile 'research'" in refusal["message"]
        assert harness.sandboxes_left() == []

    def test_without_a_profile_every_need_is_refused(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        """`[[workloads]]` declared but no default and no --profile: the run
        has no profile, and the refusal names `workload.default`."""
        harness.script([plan(needing("t1", hosts=["data.example.com"]))])
        result = harness.engine(**{**profiled, "workload": {}}).start("no profile", kind="workload")
        assert result.state == "failed"
        (refusal,) = self.refused(harness)
        assert refusal["profile"] is None
        assert refusal["key"] == "workload.default"
        assert "the run has no workload profile" in refusal["message"]

    def test_no_needs_no_grant_step(self, harness: Harness, profiled: dict[str, Any]) -> None:
        harness.script([plan(task("t1")), BUILD, PASS])
        result = harness.engine(**profiled).start("plain", kind="workload")
        assert result.state == "completed"
        assert self.granted(harness) == [] and self.refused(harness) == []
        assert harness.run_states() == WORKLOAD_STATES

    def test_a_granted_credential_re_provisions_with_the_service_box(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        """The plan asked for `weather`: the run row gains it, the pair is
        rebuilt from executing with a service sandbox holding the value,
        the agent's box never sees it, and the execute job carries the
        tool that calls through."""
        harness.script(
            [
                plan(needing("t1", credentials=["weather"])),
                {"text": "checked the forecast", "host_tool_calls": [CALL]},
                PASS,
            ]
        )
        engine = harness.engine(**profiled, keep_sandboxes=True)
        result = engine.start("what is the weather", kind="workload")
        assert result.state == "completed", result.reason
        run_id = result.run_id
        assert harness.run_states() == [
            "provisioning",
            "planning",
            "provisioning",
            "executing",
            "judging",
            "publishing",
            "completed",
        ]
        (grant,) = self.granted(harness)
        assert grant["credentials"] == ["weather"]
        assert "re-provisioning with the service sandbox" in grant["message"]
        assert engine.store.get_run(run_id).credentials == ["weather"]
        # Two boots of the agent box, one of the service box, all kept.
        names = [
            arg.removeprefix("--name=")
            for argv in harness.fake_sbx.invocations("create")
            for arg in argv
            if arg.startswith("--name=")
        ]
        assert names.count(f"sbxloop-{run_id}-agent") == 2
        assert names.count(f"sbxloop-{run_id}-service") == 1
        assert harness.sandboxes_left() == [
            f"sbxloop-{run_id}-agent",
            f"sbxloop-{run_id}-service",
        ]
        # The value went to the service box alone.
        (announce,) = [e for e in harness.events if e.type == "sandbox.service_credentials"]
        assert announce.data["name"] == f"sbxloop-{run_id}-service"
        assert announce.data["envs"] == ["WEATHER_API_KEY"]
        agent_fs = harness.fake_sbx.sandbox_fs(f"sbxloop-{run_id}-agent")
        for path in agent_fs.rglob("*"):
            if path.is_file():
                assert "wx-secret-value" not in path.read_bytes().decode("utf-8", "replace"), path
        self.assert_no_secret(harness)
        # The operator's execute job carries the tool; the plan did not.
        (execute,) = jobs(harness, run_id, "execute")
        assert [t["name"] for t in execute["host_tools"]] == ["call_service"]
        assert "## Services you may call" in execute["prompt"]
        # (the plan job's file went with the first agent box)
        # The service box answered the one call.
        service_fs = harness.fake_sbx.sandbox_fs(f"sbxloop-{run_id}-service")
        kinds = {
            json.loads(p.read_text())["kind"]
            for p in (service_fs / "home/agent/.sbxloop/jobs").iterdir()
        }
        assert kinds == {"service.http"}
        assert engine.store.get_run(run_id).state == "completed"
        # planned once: the re-provision resumed at executing
        phases = [r["phase"] for r in engine.store.phase_attempts(run_id)]
        assert phases.count("plan") == 1 and phases.count("execute") == 1

    def test_a_failed_re_provision_leaves_the_run_resumable(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        """The re-provision failing (the value is not in the daemon's env)
        leaves the plan and the credential on the row; a resume provisions
        the pair from the row and goes on from executing, planning nothing
        twice."""
        from sbxloop.errors import ProvisionError

        harness.script([plan(needing("t1", credentials=["weather"]))])
        harness.monkeypatch.delenv("WEATHER_API_KEY")
        engine = harness.engine(**profiled)
        with pytest.raises(ProvisionError, match="WEATHER_API_KEY"):
            engine.start("what is the weather", kind="workload")
        (run,) = engine.store.list_runs()
        assert run.credentials == ["weather"]
        assert run.state == "provisioning"
        assert [t.spec.id for t in engine.store.get_tasks(run.run_id)] == ["t1"]
        assert harness.sandboxes_left() == []

        harness.monkeypatch.setenv("WEATHER_API_KEY", "wx-secret-value-9f8e7d")
        harness.events.clear()
        harness.script([{"text": "checked the forecast", "host_tool_calls": [CALL]}, PASS])
        resumed = harness.engine(**profiled).resume(run.run_id)
        assert resumed.state == "completed", resumed.reason
        assert harness.run_states() == [
            "provisioning",
            "executing",
            "judging",
            "publishing",
            "completed",
        ]
        assert self.granted(harness) == [], "granted once, on the first pass"
        phases = [r["phase"] for r in engine.store.phase_attempts(run.run_id)]
        assert phases.count("plan") == 1 and phases.count("execute") == 1
        self.assert_no_secret(harness)

    def test_a_repo_the_profile_allows_is_checked_out_into_the_data_dir(
        self, harness: Harness, profiled: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop import hostgit
        from tests.unit.test_hostgit import make_repo

        upstream = make_repo(harness.tmp_path, "upstream")
        seen: list[tuple[str, str]] = []

        def fake_clone(url: str, target: Path, branch: str, **kwargs: object) -> str:
            seen.append((url, str(kwargs.get("token"))))
            return hostgit.clone_for_run(upstream, target, branch)

        monkeypatch.setattr(hostgit, "clone_from_remote", fake_clone)
        harness.script([plan(needing("t1", repo="o/docs")), BUILD, PASS])
        engine = harness.engine(
            **{**profiled, "workloads": [{**RESEARCH, "repo": True}]},
            github={"repos": [{"repo": "o/docs"}]},
        )
        result = engine.start("read the docs", kind="workload")
        assert result.state == "completed", result.reason
        assert seen == [("https://github.com/o/docs", "gh_tok")], "the host's own credential"
        checkout = harness.state_dir / "runs" / result.run_id / "workspace" / "docs"
        assert (checkout / "hello.txt").read_text() == "hi\n"
        (clone,) = [e for e in harness.events if e.type == "sandbox.workspace_clone"]
        assert clone.data["target"] == str(checkout)
        (grant,) = self.granted(harness)
        assert grant["repos"] == ["o/docs"]
        # still the one agent box; no github box, nothing delivered
        assert harness.run_states() == WORKLOAD_STATES
        assert [e for e in harness.events if e.type == "sandbox.service_credentials"] == []

    def test_a_repo_not_configured_is_refused(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        harness.script([plan(needing("t1", repo="o/elsewhere"))])
        result = harness.engine(
            **{**profiled, "workloads": [{**RESEARCH, "repo": True}]},
            github={"repos": [{"repo": "o/docs"}]},
        ).start("read the docs", kind="workload")
        assert result.state == "failed"
        (refusal,) = self.refused(harness)
        assert refusal["key"] == "github.repos"
        assert "not a configured repository" in refusal["message"]

    def test_a_repo_without_a_mount_is_refused(
        self, harness: Harness, profiled: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A checkout in a data directory the agent box cannot see would
        never be read: refused, with no key (nothing in sbxloop.toml fixes
        the mount)."""
        monkeypatch.setenv("SBX_FAKE_NO_MOUNT", "1")
        harness.script([plan(needing("t1", repo="o/docs"))])
        result = harness.engine(
            **{**profiled, "workloads": [{**RESEARCH, "repo": True}]},
            github={"repos": [{"repo": "o/docs"}]},
        ).start("read the docs", kind="workload")
        assert result.state == "failed"
        (refusal,) = self.refused(harness)
        assert refusal["key"] is None
        assert "not mounted" in refusal["message"]

    def test_a_named_profile_is_pinned_for_the_resume(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        """`--profile` chooses; the choice and its budgets persist in the
        run's config, so a resume under the same file sees no drift and a
        code run ignores the sections entirely."""
        bare = {"name": "bare", "budgets": {"max_tasks": 2}}
        overrides = {**profiled, "workloads": [RESEARCH, bare]}
        harness.script([plan(task("t1")), BUILD, PASS])
        engine = harness.engine(**overrides)
        result = engine.start("plain", kind="workload", profile="bare")
        assert result.state == "completed"
        stored = json.loads(engine.store.get_run_config(result.run_id))
        assert stored["workload"]["default"] == "bare"
        assert stored["budgets"]["max_tasks"] == 2
        assert [p["name"] for p in stored["workloads"]] == ["research", "bare"]
        # a resume under the same config file: no drift
        engine.store.set_run_state(result.run_id, "judging")
        harness.events.clear()
        harness.script([PASS])
        resumed = harness.engine(**overrides).resume(result.run_id)
        assert resumed.state == "completed"
        assert HostEventTypes.RUN_CONFIG_DRIFT not in harness.event_types()

    def test_a_profile_on_a_code_run_is_refused(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        from sbxloop.errors import ConfigError

        engine = harness.engine(**profiled)
        with pytest.raises(ConfigError, match="--kind workload"):
            engine.start("ship it", profile="research")
        assert engine.store.list_runs() == []

    def test_an_unknown_profile_is_refused_before_the_run_row(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        from sbxloop.errors import ConfigError

        engine = harness.engine(**profiled)
        with pytest.raises(ConfigError, match="'nope' is not declared"):
            engine.start("plain", kind="workload", profile="nope")
        assert engine.store.list_runs() == []


PUBLISHING = {**RESEARCH, "sinks": ["chat", "issue", "artifact", "pr"]}


class TestSinks:
    """Where the result goes (#759): the sink each task declared, the
    record of every delivery, and a resume that never delivers twice."""

    def published(self, harness: Harness) -> list[dict[str, Any]]:
        return [e.data for e in harness.events if e.type == HostEventTypes.RUN_PUBLISHED]

    def refused(self, harness: Harness) -> list[dict[str, Any]]:
        return [e.data for e in harness.events if e.type == HostEventTypes.RUN_NEEDS_REFUSED]

    def test_chat_is_the_default_sink_and_needs_no_profile(self, harness: Harness) -> None:
        """A run with no profile still publishes: the tasks' results go to
        chat, as the `run.published` event whose message is the reply."""
        harness.script(
            [
                plan(task("t1"), task("t2", deps=["t1"]), title="Two things"),
                {"text": "## Result\nwrote a", "files": {"a.txt": "a\n"}},
                PASS,
                {"text": "## Result\nread a"},
                PASS,
            ]
        )
        engine = harness.engine()
        result = engine.start("two things", kind="workload")
        assert result.state == "completed"
        (posted,) = self.published(harness)
        assert (posted["sink"], posted["location"], posted["tasks"], posted["files"]) == (
            "chat",
            "chat",
            ["t1", "t2"],
            1,
        )
        assert posted["message"] == (
            "Two things — 2/2 task(s) passed the judge\n"
            "t1: wrote a (1 file)\n"
            "t2: read a\n\n"
            "## t1: Task t1\n\nwrote a\n\nFiles: `a.txt`\n\n"
            "## t2: Task t2\n\nread a"
        )
        assert [p.sink for p in result.published] == ["chat"]
        assert [p.sink for p in engine.store.get_run(result.run_id).published] == ["chat"]
        assert [e for e in harness.events if e.type == HostEventTypes.RUN_NEEDS_GRANTED] == []
        # a workload's artifacts are what the artifact sink delivered: nothing
        assert [e for e in harness.events if e.type == HostEventTypes.RUN_ARTIFACTS] == []

    def test_the_issue_sink_files_one_result_issue(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        fake = FakeGithub()
        harness.script(
            [
                plan(needing("t1", sink="issue"), needing("t2", sink="issue"), title="Digest"),
                {"text": "## Result\nfirst half"},
                PASS,
                {"text": "## Result\nsecond half"},
                PASS,
            ]
        )
        engine = harness.engine(
            **{**profiled, "workloads": [PUBLISHING]},
            ops=fake,
            github={"repo": fake.repo},
            keep_sandboxes=True,
        )
        result = engine.start("write the digest", kind="workload")
        assert result.state == "completed", result.reason
        # the profile's sinks need GitHub, so the run got its github box
        assert harness.sandboxes_left() == [
            f"sbxloop-{result.run_id}-agent",
            f"sbxloop-{result.run_id}-github",
        ]
        ((title, body, labels),) = fake.issues_created
        assert title == "Digest"
        assert labels == ["sbxloop:result"]
        assert "sbxloop:result" in fake.labels_created
        assert body == (
            "Digest — 2/2 task(s) passed the judge\n"
            "t1: first half\n"
            "t2: second half\n\n"
            "## t1: Task t1\n\nfirst half\n\n"
            "## t2: Task t2\n\nsecond half\n\n"
            f"---\n*sbxloop run `{result.run_id}`*\n\n**Asked:** write the digest\n"
        )
        (posted,) = self.published(harness)
        url = f"https://github.com/{fake.repo}/issues/901"
        assert (posted["sink"], posted["location"], posted["tasks"]) == ("issue", url, ["t1", "t2"])
        assert posted["message"] == f"result filed as {url}"
        assert [(p.sink, p.location) for p in result.published] == [("issue", url)]
        assert result.pr_number is None and fake.pr_create_calls == 0
        (grant,) = [e for e in harness.events if e.type == HostEventTypes.RUN_NEEDS_GRANTED]
        assert grant.data["sinks"] == ["issue"]

    def test_a_custom_result_label(self, harness: Harness, profiled: dict[str, Any]) -> None:
        fake = FakeGithub()
        harness.script([plan(needing("t1", sink="issue")), BUILD, PASS])
        engine = harness.engine(
            **{
                **profiled,
                "workloads": [PUBLISHING],
                "workload": {"default": "research", "result_label": "loop:out"},
            },
            ops=fake,
            github={"repo": fake.repo},
        )
        result = engine.start("write it up", kind="workload")
        assert result.state == "completed", result.reason
        ((_, _, labels),) = fake.issues_created
        assert labels == ["loop:out"]

    def test_the_issue_sink_needs_a_repository(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        harness.script([plan(needing("t1", sink="issue")), BUILD, PASS])
        engine = harness.engine(**{**profiled, "workloads": [PUBLISHING]})
        result = engine.start("file it", kind="workload")
        assert result.state == "failed"
        (refusal,) = self.refused(harness)
        assert refusal["key"] == "github.repo"
        assert refusal["message"] == (
            "task t1 needs sink `issue` — no repository is configured to publish to; "
            "`github.repo` in sbxloop.toml would allow it"
        )
        assert self.published(harness) == []

    def test_the_issue_sink_is_refused_where_issues_are_disabled(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        fake = FakeGithub()
        fake.has_issues = False
        harness.script([plan(needing("t1", sink="issue")), BUILD, PASS])
        engine = harness.engine(
            **{**profiled, "workloads": [PUBLISHING]}, ops=fake, github={"repo": fake.repo}
        )
        result = engine.start("file it", kind="workload")
        assert result.state == "failed"
        (refusal,) = self.refused(harness)
        assert refusal["key"] is None
        assert f"{fake.repo} has Issues disabled" in refusal["message"]
        assert fake.issues_created == []

    def test_a_sink_outside_the_profile_is_refused(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        harness.script([plan(needing("t1", sink="artifact")), BUILD, PASS])
        result = harness.engine(**profiled).start("keep the files", kind="workload")
        assert result.state == "failed"
        (refusal,) = self.refused(harness)
        assert refusal["key"] == "workloads.research.sinks"
        assert (refusal["need"], refusal["value"]) == ("sink", "artifact")

    def test_the_pr_sink_is_not_available_yet(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        fake = FakeGithub()
        harness.script([plan(needing("t1", sink="pr")), BUILD, PASS])
        engine = harness.engine(
            **{**profiled, "workloads": [PUBLISHING]}, ops=fake, github={"repo": fake.repo}
        )
        result = engine.start("deliver it", kind="workload")
        assert result.state == "failed"
        (refusal,) = self.refused(harness)
        assert refusal["key"] is None and "not available yet" in refusal["message"]

    @pytest.mark.parametrize("mounted", [True, False])
    def test_the_artifact_sink_delivers_the_declared_files_only(
        self,
        harness: Harness,
        profiled: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        mounted: bool,
    ) -> None:
        """Two tasks write; the artifact directory holds the artifact
        task's files and nothing of the other's — mounted (a host copy)
        or not (a tar of the listed paths, beside the data directory's
        salvage)."""
        if not mounted:
            monkeypatch.setenv("SBX_FAKE_NO_MOUNT", "1")
        harness.script(
            [
                plan(needing("t1", sink="artifact"), needing("t2")),
                {"text": "## Result\nwrote the report", "files": {"out/report.csv": "1,2\n"}},
                PASS,
                {"text": "## Result\nscratch", "files": {"scratch.txt": "x\n"}},
                PASS,
            ]
        )
        engine = harness.engine(**{**profiled, "workloads": [PUBLISHING]})
        result = engine.start("report", kind="workload")
        assert result.state == "completed", result.reason
        assert result.mounted is mounted
        target = harness.state_dir / "runs" / result.run_id / "artifacts"
        assert sorted(
            p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()
        ) == ["out/report.csv"]
        assert (target / "out/report.csv").read_text() == "1,2\n"
        if not mounted:
            salvage = harness.state_dir / "runs" / result.run_id / "data"
            assert (salvage / "scratch.txt").read_text() == "x\n"
        delivered, posted = self.published(harness)
        assert (
            delivered["sink"],
            delivered["location"],
            delivered["tasks"],
            delivered["files"],
        ) == (
            "artifact",
            str(target),
            ["t1"],
            1,
        )
        assert delivered["message"] == f"1 file delivered to {target}"
        assert posted["sink"] == "chat" and posted["tasks"] == ["t2"]
        # `sbxloop artifacts` reads what the sink delivered, not the data dir
        report = [e for e in harness.events if e.type == HostEventTypes.RUN_ARTIFACTS][-1]
        assert report.data["path"] == str(target) and report.data["files"] == 1
        from sbxloop.engine.model import artifacts_dir

        assert artifacts_dir(result, harness.state_dir) == target

    def test_a_resume_at_publishing_never_delivers_twice(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        fake = FakeGithub()
        harness.script([plan(needing("t1", sink="issue"), needing("t2")), BUILD, PASS, BUILD, PASS])
        engine = harness.engine(
            **{**profiled, "workloads": [PUBLISHING]}, ops=fake, github={"repo": fake.repo}
        )
        result = engine.start("file it", kind="workload")
        assert result.state == "completed", result.reason
        assert len(fake.issues_created) == 1
        # Park the run as if it had died after the issue, before the chat.
        engine.store.set_run_state(result.run_id, "publishing")
        engine.store._conn.execute(
            "UPDATE runs SET published = ? WHERE run_id = ?",
            (json.dumps([result.published[0].model_dump(mode="json")]), result.run_id),
        )
        engine.store._conn.commit()
        harness.events.clear()
        harness.script([])
        resumed = harness.engine(
            **{**profiled, "workloads": [PUBLISHING]}, ops=fake, github={"repo": fake.repo}
        ).resume(result.run_id)
        assert resumed.state == "completed", resumed.reason
        assert len(fake.issues_created) == 1, "one issue per run"
        assert [p["sink"] for p in self.published(harness)] == ["chat"]
        assert [p.sink for p in resumed.published] == ["issue", "chat"]
        assert harness.run_states() == ["provisioning", "publishing", "completed"]

    def test_a_sink_that_fails_fails_the_run_named(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        from sbxloop.errors import GithubOpsError

        fake = FakeGithub()
        fake.fail_once["issue_create"] = GithubOpsError("boom (HTTP 502)", http_status=502)
        harness.script([plan(needing("t1", sink="issue")), BUILD, PASS])
        engine = harness.engine(
            **{**profiled, "workloads": [PUBLISHING]}, ops=fake, github={"repo": fake.repo}
        )
        result = engine.start("file it", kind="workload")
        assert result.state == "failed"
        assert result.reason == "publishing to issue failed: boom (HTTP 502)"
        assert result.published == [] and self.published(harness) == []
        # resumable where it stopped: the retry files the issue
        harness.events.clear()
        harness.script([])
        resumed = harness.engine(
            **{**profiled, "workloads": [PUBLISHING]}, ops=fake, github={"repo": fake.repo}
        ).resume(result.run_id)
        assert resumed.state == "completed", resumed.reason
        assert len(fake.issues_created) == 1

    def test_an_unsafe_declared_path_fails_the_artifact_sink(
        self, harness: Harness, profiled: dict[str, Any]
    ) -> None:
        harness.script([plan(needing("t1", sink="artifact")), FILES_BUILD, PASS])
        engine = harness.engine(**{**profiled, "workloads": [PUBLISHING]})
        result = engine.start("report", kind="workload")
        assert result.state == "completed", result.reason
        # Corrupt the persisted row the way only a broken store could, and
        # re-enter publishing.
        (t1,) = engine.store.get_tasks(result.run_id)
        assert t1.output is not None
        row = {**t1.output.model_dump(mode="json"), "files": ["../escape"]}
        engine.store._conn.execute(
            "UPDATE tasks SET output_json = ? WHERE run_id = ? AND task_id = ?",
            (json.dumps(row), result.run_id, "t1"),
        )
        engine.store._conn.execute(
            "UPDATE runs SET published = '[]', state = 'publishing' WHERE run_id = ?",
            (result.run_id,),
        )
        engine.store._conn.commit()
        harness.script([])
        resumed = harness.engine(**{**profiled, "workloads": [PUBLISHING]}).resume(result.run_id)
        assert resumed.state == "failed"
        assert resumed.reason == (
            "publishing to artifact failed: task t1 declared an unsafe path '../escape'"
        )
