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
        for command in ("run", "resume", "status", "logs", "doctor", "sandbox", "config"):
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

        from sbxloop.cli.app import _artifact_files, _artifacts_tree

        root = tmp_path / "ws"
        (root / "sub").mkdir(parents=True)
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text("ref")
        (root / ".hidden").write_text("x")
        for i in range(5):
            (root / f"f{i}.txt").write_text("x" * 2048)
        (root / "sub" / "nested.txt").write_text("y")

        files = _artifact_files(root)
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
    def test_dashboard_renders_state(self) -> None:
        from rich.console import Console

        from sbxloop.cli.tui import Dashboard

        dashboard = Dashboard()
        for event in [
            Event.now("run.start", "r1", outcome="big goal"),
            Event.now("run.state", "r1", state="running"),
            Event.now("task.start", "r1", task_id="t1", title="First task"),
            Event.now("task.state", "r1", task_id="t1", state="executing", revisions=1, replans=0),
            Event.now("agent.message", "r1", content="working on it"),
        ]:
            dashboard.on_event(event)

        console = Console(record=True, width=100)
        console.print(dashboard.renderable())
        text = console.export_text()
        assert "r1" in text
        assert "running" in text
        assert "First task" in text
        assert "executing" in text
        assert "working on it" in text

    def test_agent_messages_render_as_wrapped_markdown_panels(self) -> None:
        """Field complaint: ```json blocks flew by truncated and unwrapped.
        Agent messages must render as markdown (code blocks intact, long
        lines wrapped), not as clipped single lines."""
        from rich.console import Console

        from sbxloop.cli.tui import Dashboard

        long_value = "x" * 200  # far beyond one terminal row
        content = (
            "Here is the plan:\n\n"
            "```json\n"
            f'{{"tasks": [{{"id": "t1", "note": "{long_value}"}}]}}\n'
            "```"
        )
        dashboard = Dashboard()
        dashboard.on_event(Event.now("agent.message", "r1", content=content))
        console = Console(record=True, width=80)
        console.print(dashboard.renderable())
        text = console.export_text()
        assert "agent" in text  # chat bubble title
        assert '"tasks"' in text  # code block content survived
        # the long value wrapped instead of being clipped: all 200 chars
        # of payload are present in the output across multiple lines
        assert text.count("x") >= 200

    def test_deltas_and_heartbeats_stay_out_of_transcript(self) -> None:
        from sbxloop.cli.tui import Dashboard

        dashboard = Dashboard()
        dashboard.on_event(Event.now("agent.message_delta", "r1", delta="chunk"))
        dashboard.on_event(Event.now("worker.heartbeat", "r1"))
        dashboard.on_event(Event.now("worker.stdout", "r1", line="noise"))
        assert len(dashboard.transcript) == 0

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
