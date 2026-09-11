"""A ``tool`` run: a fixed recipe on the run shape, with no agent in it.

The recipe seeds the task graph — each task a command the host chose, its
checks, the files it leaves — and the engine runs the command as a shell
job in the data directory, checks it, and hands the files to the sinks.
The assertions here are that nothing else happens: no agent session is
ever submitted, the chronology is the command's and not a model's, and
"did it work" is decided by the recipe's own exit criterion alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sbxloop.engine.model import TOOL_STAGES, TaskNeeds, TaskSpec
from sbxloop.errors import ConfigError
from sbxloop.events import HostEventTypes
from tests.conftest import FakeSbx
from tests.unit.test_engine import Harness

TOOL_STATES = ["provisioning", "executing", "publishing", "completed"]

REPORT = "# Findings\n\nOne entrypoint, one path.\n"


def tool(
    command: str,
    *,
    checks: list[str] | None = None,
    files: list[str] | None = None,
    hosts: list[str] | None = None,
    repo: str | None = None,
) -> TaskSpec:
    return TaskSpec(
        id="scan",
        title="scan the tree",
        command=command,
        verify_commands=checks if checks is not None else ["test -s out/report.json"],
        result_files=files if files is not None else ["out/report.md", "out/report.json"],
        needs=TaskNeeds(hosts=hosts or [], repo=repo, sink="chat"),
    )


# A command that leaves what the recipe declared: a Markdown report and a
# JSON twin, the way the entrygraph scanner does.
WRITE_REPORTS = (
    "mkdir -p out && printf '# Findings\\n\\nOne entrypoint, one path.\\n' > out/report.md"
    " && printf '{}' > out/report.json"
)


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


def published(harness: Harness) -> list[dict[str, Any]]:
    return [e.data for e in harness.events if e.type == HostEventTypes.RUN_PUBLISHED]


class TestToolRun:
    def test_the_command_runs_its_checks_pass_and_the_files_reach_chat(
        self, harness: Harness
    ) -> None:
        """The whole life of a tool run, and what it never does."""
        harness.script([])  # no agent entry is ever consumed
        engine = harness.engine(keep_sandboxes=True)
        result = engine.start("scan the tree", kind="tool", tasks=[tool(WRITE_REPORTS)])
        assert result.state == "completed", result.reason
        assert result.kind == "tool"
        assert harness.run_states() == TOOL_STATES
        assert harness.consumed() == 0
        # One sandbox, and every job it received was a shell job.
        assert harness.sandboxes_left() == [f"sbxloop-{result.run_id}-agent"]
        kinds = {job["kind"] for job in harness.agent_jobs(result.run_id)}
        assert kinds == {"shell.batch"}
        # The task's output is what the command left, read by code.
        (task,) = result.tasks
        assert task.state == "done" and task.output is not None
        assert task.output.files == ["out/report.md", "out/report.json"]
        assert task.output.summary == "# Findings"
        assert "One entrypoint, one path." in task.output.text
        # The chat sink carried both files and the report's own words.
        (entry,) = published(harness)
        assert entry["sink"] == "chat" and entry["tasks"] == ["scan"]
        assert {Path(p).name for p in entry["paths"]} == {"report.md", "report.json"}
        assert "One entrypoint, one path." in entry["message"]
        assert result.summary is not None
        assert result.summary.startswith("1/1 command(s) passed their checks")

    def test_stages_are_the_two_and_the_states_record_no_judgment(self, harness: Harness) -> None:
        assert TOOL_STAGES == ("executing", "publishing")
        harness.script([])
        result = harness.engine().start("scan", kind="tool", tasks=[tool(WRITE_REPORTS)])
        assert "judging" not in harness.run_states() and "planning" not in harness.run_states()
        phases = {(r.phase, r.status) for r in result_phases(harness, result.run_id)}
        assert phases == {("execute", "ok")}

    def test_a_failed_command_fails_the_run_named(self, harness: Harness) -> None:
        harness.script([])
        result = harness.engine().start("scan", kind="tool", tasks=[tool("echo boom >&2; exit 3")])
        assert result.state == "failed"
        # One line, the task named, the command not repeated (it is the
        # recipe's and long), the output's last line as the detail.
        assert result.reason == "task scan: command exited 3 — boom"
        (task,) = result.tasks
        assert task.state == "failed" and task.output is None
        assert published(harness) == []
        assert harness.run_states() == ["provisioning", "executing", "failed"]

    def test_a_failed_check_fails_the_run_named(self, harness: Harness) -> None:
        harness.script([])
        result = harness.engine().start(
            "scan",
            kind="tool",
            tasks=[tool(WRITE_REPORTS, checks=["grep -q nothing-here out/report.md"])],
        )
        assert result.state == "failed"
        assert result.reason == "task scan: check `grep -q nothing-here out/report.md` exited 1"
        assert published(harness) == []

    def test_a_failure_reason_is_one_line_never_the_transcript(self, harness: Harness) -> None:
        """The reason is what the finish card posts in a chat thread. A
        traceback ends with the exception and a tool with its error line;
        that line is the reason, and the rest stays on the phase row."""
        harness.script([])
        command = (
            'printf \'Traceback (most recent call last):\\n  File "scan.py", line 1\\n'
            "    boom()\\nRuntimeError: the clone was refused\\n' >&2; exit 1"
        )
        engine = harness.engine()
        result = engine.start("scan", kind="tool", tasks=[tool(command)])
        assert result.state == "failed"
        assert result.reason == "task scan: command exited 1 — RuntimeError: the clone was refused"
        assert "Traceback" not in result.reason and "printf" not in result.reason
        # The whole transcript is on the phase row for whoever needs it.
        (row,) = engine.store.phase_attempts(result.run_id)
        assert row.phase == "execute" and row.status == "failed"
        assert row.output_json is not None and "Traceback" in row.output_json
        # The chronology's phase line is the same one line.
        (ended,) = [
            e
            for e in harness.events
            if e.type == HostEventTypes.PHASE_END and e.data.get("phase") == "execute"
        ]
        assert ended.data["message"] == result.reason

    def test_a_missing_declared_file_fails_closed(self, harness: Harness) -> None:
        """A check that passed while a file the sink carries is missing is
        a failure, not a partial result."""
        harness.script([])
        result = harness.engine().start(
            "scan",
            kind="tool",
            tasks=[tool("mkdir -p out && printf '{}' > out/report.json")],
        )
        assert result.state == "failed"
        assert result.reason is not None and "`out/report.md`" in result.reason
        assert published(harness) == []

    def test_declared_hosts_are_granted_before_the_command(self, harness: Harness) -> None:
        harness.script([])
        result = harness.engine().start(
            "scan",
            kind="tool",
            tasks=[tool(WRITE_REPORTS, hosts=["grammars.example", "index.example"])],
        )
        assert result.state == "completed", result.reason
        granted = [
            e.data
            for e in harness.events
            if e.type == HostEventTypes.POLICY_ALLOW and e.data.get("task_id") == "scan"
        ]
        assert [g["domain"] for g in granted] == ["grammars.example", "index.example"]
        assert all(g["reason"] == "declared by the recipe" for g in granted)

    def test_a_denied_host_fails_the_run_closed(self, harness: Harness) -> None:
        """`[policy] deny` wins over a recipe's declaration, as it wins over
        an agent's."""
        harness.script([])
        result = harness.engine(policy={"deny": ["evil.example"]}).start(
            "scan", kind="tool", tasks=[tool(WRITE_REPORTS, hosts=["evil.example"])]
        )
        assert result.state == "failed"
        assert result.reason is not None and "evil.example" in result.reason

    def test_an_unconfigured_repository_fails_before_anything_runs(self, harness: Harness) -> None:
        harness.script([])
        engine = harness.engine(keep_sandboxes=True)
        result = engine.start("scan", kind="tool", tasks=[tool(WRITE_REPORTS, repo="org/private")])
        assert result.state == "failed"
        assert result.reason == (
            "task scan needs repository `org/private`, which is not configured"
        )
        assert harness.agent_jobs(result.run_id) == []

    def test_a_tool_run_without_a_command_is_refused_before_the_run_row(
        self, harness: Harness
    ) -> None:
        harness.script([])
        engine = harness.engine()
        with pytest.raises(ConfigError, match="carry a command"):
            engine.start("scan", kind="tool", tasks=[TaskSpec(id="t", title="t")])
        with pytest.raises(ConfigError, match="carry a command"):
            engine.start("scan", kind="tool")
        assert harness.run_states() == []

    def test_a_tool_run_requires_its_mount(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The recipe staged the run's inputs; a sandbox that came up
        without them has no work."""
        from sbxloop.errors import ProvisionError

        monkeypatch.setenv("SBX_FAKE_NO_MOUNT", "1")
        harness.script([])
        with pytest.raises(ProvisionError, match="mount"):
            harness.engine().start("scan", kind="tool", tasks=[tool(WRITE_REPORTS)])

    def test_a_resume_at_publishing_does_not_run_the_command_again(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The command ran and its files are on the host; a run interrupted
        while publishing re-enters there, on a fresh sandbox that receives
        no shell job."""
        from sbxloop.engine.engine import LoopEngine

        harness.script([])
        engine = harness.engine()
        original = LoopEngine._stage_publish
        calls: list[str] = []

        def interrupted(self: LoopEngine, p: Any) -> str | None:
            calls.append("publish")
            if len(calls) == 1:
                # Entered the stage, then died in it — a bridge outage, a
                # host restart — before any sink recorded a delivery.
                self._set_run_state(p.run_id, "publishing")
                raise RuntimeError("the bridge went away")
            return original(self, p)

        monkeypatch.setattr(LoopEngine, "_stage_publish", interrupted)
        with pytest.raises(RuntimeError):
            engine.start("scan", run_id="rtool", kind="tool", tasks=[tool(WRITE_REPORTS)])
        assert engine.store.get_run("rtool").stage == "publishing"
        result = engine.resume("rtool")
        assert result.state == "completed", result.reason
        assert calls == ["publish", "publish"]
        (entry,) = published(harness)
        assert entry["sink"] == "chat"
        # One execute row and one execute phase end across both attempts:
        # nothing was re-run.
        executed = [
            e
            for e in harness.events
            if e.type == HostEventTypes.PHASE_END and e.data.get("phase") == "execute"
        ]
        assert len(executed) == 1
        assert [r.phase for r in engine.store.phase_attempts("rtool")] == ["execute"]

    def test_a_tool_run_holds_no_credentials(self, harness: Harness) -> None:
        harness.script([])
        engine = harness.engine(
            credentials=[{"name": "weather", "host": "api.example", "env": "WEATHER_API_KEY"}]
        )
        with pytest.raises(ConfigError, match="no credentials"):
            engine.start("scan", kind="tool", tasks=[tool(WRITE_REPORTS)], credentials=["weather"])


def result_phases(harness: Harness, run_id: str) -> list[Any]:
    engine_store = harness.engine().store
    return list(engine_store.phase_attempts(run_id))
