"""CLI tests via typer's CliRunner, fake sbx, and the scripted echo backend."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import sbxloop
from sbxloop.cli.app import app
from sbxloop.engine.store import StateStore
from sbxloop.events import Event
from sbxloop_worker.protocol import Event as ProtocolEvent
from tests.conftest import FakeSbx

runner = CliRunner()


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def seed_store(workdir: Path) -> StateStore:
    store = StateStore(workdir / ".sbxloop" / "state.db")
    store.create_run("rseeded11", "make everything better")
    store.set_run_state("rseeded11", "completed")
    from sbxloop.engine.model import TaskSpec

    store.save_tasks("rseeded11", [TaskSpec(id="t1", title="Task one")])
    store.append_event(
        ProtocolEvent(ts=1.0, run_id="rseeded11", type="run.start", data={"outcome": "x"})
    )
    store.append_event(
        ProtocolEvent(
            ts=2.0, run_id="rseeded11", type="task.state", data={"task_id": "t1", "state": "done"}
        )
    )
    store.append_event(
        ProtocolEvent(ts=3.0, run_id="rseeded11", type="run.end", data={"state": "completed"})
    )
    return store


class TestBasics:
    def test_version(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert sbxloop.__version__ in result.output

    def test_help_lists_commands(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for command in (
            "run",
            "resume",
            "status",
            "logs",
            "artifacts",
            "doctor",
            "sandbox",
            "config",
        ):
            assert command in result.output


class TestStatusAndLogs:
    def test_status_lists_runs(self, workdir: Path) -> None:
        seed_store(workdir)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "rseeded11" in result.output
        assert "completed" in result.output

    def test_status_run_detail(self, workdir: Path) -> None:
        seed_store(workdir)
        result = runner.invoke(app, ["status", "rseeded11"])
        assert result.exit_code == 0
        assert "Task one" in result.output

    def test_status_unknown_run(self, workdir: Path) -> None:
        seed_store(workdir)
        result = runner.invoke(app, ["status", "rghost"])
        assert result.exit_code == 2

    def test_logs_replay_and_filters(self, workdir: Path) -> None:
        seed_store(workdir)
        result = runner.invoke(app, ["logs", "rseeded11"])
        assert result.exit_code == 0
        assert "run.start" in result.output
        assert "task.state" in result.output

        filtered = runner.invoke(app, ["logs", "rseeded11", "--type", "task."])
        assert "run.start" not in filtered.output
        assert "task.state" in filtered.output

    def test_logs_follow_stops_on_terminal_run(self, workdir: Path) -> None:
        seed_store(workdir)
        result = runner.invoke(app, ["logs", "rseeded11", "--follow"])
        assert result.exit_code == 0
        assert "run.end" in result.output


class TestArtifactsCommand:
    def seed_with_workspace(self, workdir: Path, *, mounted: bool) -> Path:
        store = seed_store(workdir)
        workspace = workdir / "runs-ws"
        workspace.mkdir()
        store.set_run_workspace("rseeded11", workspace, mounted)
        return workspace

    def test_unknown_run_errors(self, workdir: Path) -> None:
        seed_store(workdir)
        result = runner.invoke(app, ["artifacts", "rghost"])
        assert result.exit_code == 2
        assert "unknown run" in result.output

    def test_never_provisioned_run_errors_cleanly(self, workdir: Path) -> None:
        seed_store(workdir)  # run exists but has no workspace recorded
        result = runner.invoke(app, ["artifacts", "rseeded11"])
        assert result.exit_code == 2
        assert "never provisioned a workspace" in result.output

    def test_mounted_run_lists_files_with_sizes(self, workdir: Path) -> None:
        workspace = self.seed_with_workspace(workdir, mounted=True)
        (workspace / "hello.txt").write_text("hi")
        (workspace / "sub").mkdir()
        (workspace / "sub" / "data.bin").write_bytes(b"x" * 2048)
        result = runner.invoke(app, ["artifacts", "rseeded11"])
        assert result.exit_code == 0
        assert "2 file(s)" in result.output
        assert "live workspace mount" in result.output
        assert "hello.txt" in result.output
        assert "2.0 KB" in result.output

    def test_path_prints_bare_directory(self, workdir: Path) -> None:
        workspace = self.seed_with_workspace(workdir, mounted=True)
        result = runner.invoke(app, ["artifacts", "rseeded11", "--path"])
        assert result.exit_code == 0
        assert result.output.strip() == str(workspace)

    def test_harvested_run_reads_artifacts_dir(self, workdir: Path) -> None:
        self.seed_with_workspace(workdir, mounted=False)
        harvested = workdir / ".sbxloop" / "runs" / "rseeded11" / "artifacts"
        harvested.mkdir(parents=True)
        (harvested / "result.md").write_text("# out")
        result = runner.invoke(app, ["artifacts", "rseeded11"])
        assert result.exit_code == 0
        assert "harvested copy" in result.output
        assert "result.md" in result.output

    def test_tree_renders(self, workdir: Path) -> None:
        workspace = self.seed_with_workspace(workdir, mounted=True)
        (workspace / "a").mkdir()
        (workspace / "a" / "deep.txt").write_text("d")
        result = runner.invoke(app, ["artifacts", "rseeded11", "--tree"])
        assert result.exit_code == 0
        assert "a/" in result.output
        assert "deep.txt" in result.output

    def test_missing_directory_errors(self, workdir: Path) -> None:
        workspace = self.seed_with_workspace(workdir, mounted=True)
        workspace.rmdir()
        result = runner.invoke(app, ["artifacts", "rseeded11"])
        assert result.exit_code == 2
        assert "gone" in result.output


class TestConfigAndInit:
    def test_config_show_sources(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (workdir / "sbxloop.toml").write_text('model = "gpt-5"\n')
        monkeypatch.setenv("SBXLOOP_KEEP_SANDBOXES", "true")
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        assert "gpt-5" in result.output
        assert "sbxloop.toml" in result.output
        assert "env" in result.output

    def test_init_writes_and_refuses_overwrite(self, workdir: Path) -> None:
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert (workdir / "sbxloop.toml").is_file()
        # the generated file must itself be valid config
        from sbxloop.config import load_config

        config = load_config(cwd=workdir, env={})
        assert config.model == "auto"

        again = runner.invoke(app, ["init"])
        assert again.exit_code == 2
        forced = runner.invoke(app, ["init", "--force"])
        assert forced.exit_code == 0


class TestSandboxCommands:
    def test_sandbox_ls_filters_sbxloop(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        from sbxloop.sbx.cli import SbxCLI
        from sbxloop.sbx.models import SandboxSpec

        cli = SbxCLI(binary=str(fake_sbx.binary))
        cli.create(SandboxSpec(name="sbxloop-r1-agent", role="agent", workspace=workdir))
        cli.create(SandboxSpec(name="unrelated", role="agent", workspace=workdir))
        result = runner.invoke(app, ["sandbox", "ls"])
        assert result.exit_code == 0
        assert "sbxloop-r1-agent" in result.output
        assert "unrelated" not in result.output

    def test_sandbox_rm_by_run(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        from sbxloop.sbx.cli import SbxCLI
        from sbxloop.sbx.models import SandboxSpec

        cli = SbxCLI(binary=str(fake_sbx.binary))
        for role in ("agent", "github"):
            cli.create(SandboxSpec(name=f"sbxloop-r9-{role}", role="agent", workspace=workdir))
        result = runner.invoke(app, ["sandbox", "rm", "--run", "r9"])
        assert result.exit_code == 0
        assert cli.ls() == []

    def test_sandbox_rm_requires_target(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        result = runner.invoke(app, ["sandbox", "rm"])
        assert result.exit_code == 2


class TestDoctor:
    def test_doctor_with_fake_sbx_and_tokens(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GH_TOKEN", "tok")
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "sbx binary" in result.output
        assert "FAIL" not in result.output

    def test_doctor_fails_without_tokens(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "FAIL" in result.output

    def test_doctor_ok_without_gh_token_when_github_unconfigured(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        for name in ("GH_TOKEN", "GITHUB_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "github integration" in result.output
        assert "not configured" in result.output

    def test_doctor_fails_missing_gh_token_when_github_configured(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        for name in ("GH_TOKEN", "GITHUB_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("SBXLOOP_GITHUB__REPO", "owner/repo")
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "FAIL" in result.output

    def test_doctor_without_sbx(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", str(workdir))  # nothing on PATH
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GH_TOKEN", "tok")
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "not found on PATH" in result.output


class TestRunCommand:
    def make_run_env(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, Any]]
    ) -> None:
        script = workdir / "echo-script.json"
        script.write_text(json.dumps(responses))
        monkeypatch.setenv("SBXLOOP_WORKER_BACKEND", "echo")
        monkeypatch.setenv("SBXLOOP_ECHO_SCRIPT", str(script))
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GH_TOKEN", "tok")
        monkeypatch.setenv("SBXLOOP_WORKER_PYTHON", sys.executable)
        monkeypatch.setenv("SBXLOOP_INSTALL_WORKERS", "false")

    def test_run_no_tui_completes(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.make_run_env(
            workdir,
            monkeypatch,
            [
                {
                    "json": {
                        "tasks": [
                            {
                                "id": "t1",
                                "title": "Only task",
                                "description": "",
                                "depends_on": [],
                                "acceptance_criteria": ["works"],
                                "verify_commands": ["true"],
                            }
                        ]
                    }
                },
                {"json": {"steps": ["do"], "expected_artifacts": [], "verify_commands": []}},
                {"text": "did it"},
                {"json": {"verdict": "pass"}},
                {"json": {"verdict": "accept"}},
            ],
        )
        result = runner.invoke(app, ["run", "make it so", "--no-tui"])
        assert result.exit_code == 0, result.output
        assert "finished" in result.output
        assert "completed" in result.output
        assert "t1: done" in result.output

    def test_run_tui_preserves_full_transcript_history(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The TUI must never wipe history: every agent message printed
        during the run has to be present in the final output, not just the
        last few entries of a bounded buffer."""
        messages = [f"progress report number {i}" for i in range(1, 11)]
        self.make_run_env(
            workdir,
            monkeypatch,
            [
                {
                    "json": {
                        "tasks": [
                            {
                                "id": "t1",
                                "title": "Only task",
                                "description": "",
                                "depends_on": [],
                                "acceptance_criteria": ["works"],
                                "verify_commands": ["true"],
                            }
                        ]
                    }
                },
                {"json": {"steps": ["do"], "expected_artifacts": [], "verify_commands": []}},
                {
                    "text": "did it",
                    "events": [{"type": "agent.message", "data": {"content": m}} for m in messages],
                },
                {"json": {"verdict": "pass"}},
                {"json": {"verdict": "accept"}},
            ],
        )
        result = runner.invoke(app, ["run", "make it so"])  # tui mode (default)
        assert result.exit_code == 0, result.output
        for message in messages:
            assert message in result.output

    def test_run_report_refused_without_github_config(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.make_run_env(workdir, monkeypatch, [])
        result = runner.invoke(app, ["run", "anything", "--report", "--no-tui"])
        assert result.exit_code == 2
        assert "GitHub integration is not configured" in result.output
        # refused before any sandbox was created
        assert fake_sbx.invocations("create") == []

    def test_run_report_config_without_repo_refused(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.make_run_env(workdir, monkeypatch, [])
        monkeypatch.setenv("SBXLOOP_GITHUB__REPORT", "true")
        result = runner.invoke(app, ["run", "anything", "--no-tui"])
        assert result.exit_code == 2
        assert "GitHub integration is not configured" in result.output

    def test_run_no_report_overrides_config(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # report=true in config but no repo: --no-report must make the run legal.
        self.make_run_env(
            workdir,
            monkeypatch,
            [
                {
                    "json": {
                        "tasks": [
                            {
                                "id": "t1",
                                "title": "Only task",
                                "description": "",
                                "depends_on": [],
                                "acceptance_criteria": ["works"],
                                "verify_commands": ["true"],
                            }
                        ]
                    }
                },
                {"json": {"steps": ["do"], "expected_artifacts": [], "verify_commands": []}},
                {"text": "did it"},
                {"json": {"verdict": "pass"}},
                {"json": {"verdict": "accept"}},
            ],
        )
        monkeypatch.setenv("SBXLOOP_GITHUB__REPORT", "true")
        result = runner.invoke(app, ["run", "make it so", "--no-report", "--no-tui"])
        assert result.exit_code == 0, result.output

    def test_run_summary_lists_artifacts(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.make_run_env(
            workdir,
            monkeypatch,
            [
                {
                    "json": {
                        "tasks": [
                            {
                                "id": "t1",
                                "title": "Write the file",
                                "description": "",
                                "depends_on": [],
                                "acceptance_criteria": ["works"],
                                "verify_commands": ["true"],
                            }
                        ]
                    }
                },
                {"json": {"steps": ["do"], "expected_artifacts": [], "verify_commands": []}},
                {"text": "did it", "files": {"hello.txt": "hi", "docs/readme.md": "# hi"}},
                {"json": {"verdict": "pass"}},
                {"json": {"verdict": "accept"}},
            ],
        )
        result = runner.invoke(app, ["run", "write hello", "--no-tui"])
        assert result.exit_code == 0, result.output
        assert "artifacts: 2 file(s)" in result.output
        assert "hello.txt" in result.output
        assert "readme.md" in result.output
        # the files really are on the host, inside the run workspace
        runs = list((workdir / ".sbxloop" / "runs").iterdir())
        assert len(runs) == 1
        assert (runs[0] / "workspace" / "hello.txt").read_text() == "hi"

        # ...and the artifacts command finds them after the run (full loop:
        # executor writes -> mount propagates -> store resolves -> CLI lists)
        run_id = StateStore(workdir / ".sbxloop" / "state.db").list_runs()[0].run_id
        listed = runner.invoke(app, ["artifacts", run_id])
        assert listed.exit_code == 0, listed.output
        assert "hello.txt" in listed.output
        bare = runner.invoke(app, ["artifacts", run_id, "--path"])
        assert bare.output.strip() == str(runs[0] / "workspace")

    def test_run_deliver_flag_triggers_delivery(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sbxloop.engine.engine as engine_mod
        from sbxloop.gh.ops import PrRef

        delivered: list[str] = []

        def fake_deliver(ops: Any, repo: str, **kwargs: Any) -> PrRef:
            delivered.append(repo)
            return PrRef(number=8, url="https://github.com/o/r/pull/8")

        monkeypatch.setattr(engine_mod, "deliver_workspace", fake_deliver)
        self.make_run_env(
            workdir,
            monkeypatch,
            [
                {
                    "json": {
                        "tasks": [
                            {
                                "id": "t1",
                                "title": "Only task",
                                "description": "",
                                "depends_on": [],
                                "acceptance_criteria": ["works"],
                                "verify_commands": ["true"],
                            }
                        ]
                    }
                },
                {"json": {"steps": ["do"], "expected_artifacts": [], "verify_commands": []}},
                {"text": "did it", "files": {"hello.txt": "hi"}},
                {"json": {"verdict": "pass"}},
                {"json": {"verdict": "accept"}},
            ],
        )
        result = runner.invoke(app, ["run", "ship it", "--no-tui", "--deliver", "o/r"])
        assert result.exit_code == 0, result.output
        assert delivered == ["o/r"]
        assert "run.deliver" in result.output
        assert "pull/8" in result.output

    def test_run_failure_exit_code(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # decompose fails twice -> WorkerError -> exit 2
        bad = {"json": {"tasks": [{"id": "t1"}]}}
        self.make_run_env(workdir, monkeypatch, [bad, bad])
        result = runner.invoke(app, ["run", "impossible", "--no-tui"])
        assert result.exit_code == 2
        assert "run failed" in result.output


class TestArtifactsTree:
    def test_tree_caps_and_hides_dotfiles(self, tmp_path: Path) -> None:
        from rich.console import Console

        from sbxloop.cli.app import _artifacts_tree
        from sbxloop.engine.model import artifact_files

        root = tmp_path / "ws"
        (root / "sub").mkdir(parents=True)
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text("ref")
        (root / ".hidden").write_text("x")
        for i in range(5):
            (root / f"f{i}.txt").write_text("x" * 2048)
        (root / "sub" / "nested.txt").write_text("y")

        files = artifact_files(root)
        assert [p.name for p in files if p.name.startswith(".")] == []
        assert len(files) == 6

        console = Console(record=True, width=100)
        console.print(_artifacts_tree(root, files, cap=3))
        text = console.export_text()
        assert "f0.txt" in text
        assert "2.0 KB" in text
        assert "+3 more" in text
        assert ".git" not in text

    def test_human_size_units(self) -> None:
        from sbxloop.cli.app import _human_size

        assert _human_size(3) == "3 B"
        assert _human_size(2048) == "2.0 KB"
        assert _human_size(5 * 1024 * 1024) == "5.0 MB"
        assert _human_size(3 * 1024**3) == "3072.0 MB"


class TestDashboard:
    def test_pinned_status_renders_run_and_tasks(self) -> None:
        from rich.console import Console

        from sbxloop.cli.tui import Dashboard

        dashboard = Dashboard()
        for event in [
            Event.now("run.start", "r1", outcome="big goal"),
            Event.now("run.state", "r1", state="running"),
            Event.now("task.start", "r1", task_id="t1", title="First task"),
            Event.now("task.state", "r1", task_id="t1", state="executing", revisions=1, replans=0),
        ]:
            dashboard.on_event(event)

        console = Console(record=True, width=100)
        console.print(dashboard.renderable())
        text = console.export_text()
        assert "r1" in text
        assert "running" in text
        assert "First task" in text
        assert "executing" in text

    def test_status_region_holds_no_transcript(self) -> None:
        """The pinned region must stay compact: transcript entries live in
        the terminal scrollback (printed once, never rewritten), so agent
        messages must NOT appear in the re-rendered status panel."""
        from rich.console import Console

        from sbxloop.cli.tui import Dashboard

        dashboard = Dashboard()
        dashboard.on_event(Event.now("run.state", "r1", state="running"))
        dashboard.on_event(Event.now("agent.message", "r1", content="a very chatty message"))
        console = Console(record=True, width=100)
        console.print(dashboard.renderable())
        assert "a very chatty message" not in console.export_text()

    def test_agent_messages_render_as_wrapped_markdown_panels(self) -> None:
        """Field complaint: ```json blocks flew by truncated and unwrapped.
        Agent messages must render as markdown (code blocks intact, long
        lines wrapped), not as clipped single lines."""
        from rich.console import Console

        from sbxloop.cli.tui import render_event

        long_value = "x" * 200  # far beyond one terminal row
        content = (
            "Here is the plan:\n\n"
            "```json\n"
            f'{{"tasks": [{{"id": "t1", "note": "{long_value}"}}]}}\n'
            "```"
        )
        rendered = render_event(Event.now("agent.message", "r1", content=content))
        assert rendered is not None
        console = Console(record=True, width=80)
        console.print(rendered)
        text = console.export_text()
        assert "agent" in text  # chat bubble title
        assert '"tasks"' in text  # code block content survived
        # the long value wrapped instead of being clipped: all 200 chars
        # of payload are present in the output across multiple lines
        assert text.count("x") >= 200

    def test_deltas_and_heartbeats_stay_out_of_transcript(self) -> None:
        from sbxloop.cli.tui import render_event

        assert render_event(Event.now("agent.message_delta", "r1", delta="chunk")) is None
        assert render_event(Event.now("worker.heartbeat", "r1")) is None
        assert render_event(Event.now("worker.stdout", "r1", line="noise")) is None

    def test_worker_error_renders_red_panel(self) -> None:
        from rich.console import Console

        from sbxloop.cli.tui import render_event

        rendered = render_event(
            Event.now("worker.error", "r1", error_type="RuntimeError", message="boom happened")
        )
        assert rendered is not None
        console = Console(record=True, width=80)
        console.print(rendered)
        text = console.export_text()
        assert "error" in text
        assert "boom happened" in text

    def test_format_event_variants(self) -> None:
        from sbxloop.cli.tui import format_event

        line = format_event(Event.now("task.end", "r1", task_id="t1", state="done"))
        assert "task.end" in line
        assert "[t1]" in line
        assert "done" in line

    def test_format_event_includes_tool_args(self) -> None:
        from sbxloop.cli.tui import format_event

        line = format_event(
            Event.now("agent.tool_start", "r1", tool="bash", args="pip install -e .")
        )
        assert "bash" in line
        assert "pip install -e ." in line


class TestToolTranscript:
    def render_text(self, event: Event) -> str | None:
        from rich.console import Console

        from sbxloop.cli.tui import render_event

        rendered = render_event(event)
        if rendered is None:
            return None
        console = Console(record=True, width=100)
        console.print(rendered)
        return console.export_text()

    def test_tool_start_shows_command(self) -> None:
        text = self.render_text(
            Event.now("agent.tool_start", "r1", tool="bash", args="python -m pytest -q")
        )
        assert text is not None
        assert "⚙ bash" in text
        assert "python -m pytest -q" in text

    def test_tool_start_without_args_still_renders(self) -> None:
        text = self.render_text(Event.now("agent.tool_start", "r1", tool="str_replace_editor"))
        assert text is not None
        assert "⚙ str_replace_editor" in text
        assert "$" not in text

    def test_tool_start_long_command_elided_to_one_line(self) -> None:
        long_cmd = "python -c '" + "x" * 500 + "'"
        text = self.render_text(Event.now("agent.tool_start", "r1", tool="bash", args=long_cmd))
        assert text is not None
        assert "…" in text

    def test_tool_end_success_is_quiet_check(self) -> None:
        text = self.render_text(
            Event.now("agent.tool_end", "r1", tool="bash", success=True, exit_code=0)
        )
        assert text is not None
        assert "✓ bash" in text
        assert "✗" not in text

    def test_tool_end_failure_shows_exit_and_tail(self) -> None:
        text = self.render_text(
            Event.now(
                "agent.tool_end",
                "r1",
                tool="bash",
                success=False,
                exit_code=2,
                output="line1\nline2\nE: broken",
            )
        )
        assert text is not None
        assert "✗ bash exit 2" in text
        assert "E: broken" in text

    def test_tool_end_without_signal_is_skipped(self) -> None:
        # Older/other backends may emit bare completions; stay quiet then.
        assert self.render_text(Event.now("agent.tool_end", "r1", tool_call_id="c1")) is None


class TestDoctorRendering:
    def test_multiline_error_detail_is_flattened(self) -> None:
        from sbxloop.cli.doctor import _clean

        messy = "sbx ls failed | rc=1 | stderr=line one\nline two\n\n   line three"
        cleaned = _clean(messy)
        assert "\n" not in cleaned
        assert "line one line two line three" in cleaned

    def test_overlong_detail_is_elided(self) -> None:
        from sbxloop.cli.doctor import _clean

        cleaned = _clean("x" * 1000)
        assert len(cleaned) == 300
        assert cleaned.endswith("…")

    def test_doctor_emits_progress_lines(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GH_TOKEN", "tok")
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "checking sbx binary" in result.output
        assert "browser window" in result.output  # auth heads-up is visible

    def test_doctor_login_hint_names_app_name_when_configured(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GH_TOKEN", "tok")
        monkeypatch.setenv("SBXLOOP_APP_NAME", "sbxloop-iso")
        fake_sbx.script("ls", returncode=1, stderr="not logged in", once=True)
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        # the table may fold the hint across lines; assert on whole words
        assert "--app-name" in result.output
        assert "sbxloop-iso" in result.output
