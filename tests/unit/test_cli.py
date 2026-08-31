"""CLI tests via typer's CliRunner, fake sbx, and the scripted echo backend."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

import sbxloop
from sbxloop.cli.app import app
from sbxloop.cli.doctor import Check
from sbxloop.daemon.model import WorkItem
from sbxloop.engine.store import StateStore
from sbxloop.events import Event
from sbxloop_worker.protocol import Event as ProtocolEvent
from tests.conftest import FakeSbx
from tests.fakes.fake_github import FakeGithub

runner = CliRunner()

# A verbatim-shaped bash tool call from a real run thread: the informative
# verb sits behind a long, per-run `cd` prefix (#403).
RUN_PATH = "/home/bergs/.local/state/sbxloop/sbxloop-work/runs/rfxm7ad23/workspace"
RUN_CMD = (
    f"cd {RUN_PATH} && git diff --stat -- README.md docs/architecture.md CHANGELOG.md | head -120"
)


def assert_no_silent_truncation(rendered: str) -> None:
    """Every token of the original command survives whole unless it carries
    an explicit `…` marker — truncation must never split mid-token."""
    whole = set(RUN_CMD.split()) | {"$RUN"}
    command = rendered.split(" $ ", 1)[-1].splitlines()[0]
    for token in command.split():
        if "…" in token:
            continue
        assert token in whole or not any(
            token != cand and cand.startswith(token) for cand in whole
        ), f"token {token!r} looks like a silently truncated fragment"


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    # The daemon anchors its default state dir under XDG state home (#255);
    # keep that inside tmp so tests never touch the real ~/.local/state.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
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
            "bake",
            "list-models",
            "shell",
            "doctor",
            "sandbox",
            "config",
        ):
            assert command in result.output

    def test_run_and_resume_offer_chat_toggle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Wide terminal: rich wraps 80-col help panels mid-token in CI,
        # splitting "--no-chat" across lines.
        monkeypatch.setenv("COLUMNS", "300")
        for command in ("run", "resume"):
            result = runner.invoke(app, [command, "--help"])
            assert result.exit_code == 0
            # GitHub Actions forces typer's terminal colors on, and the
            # option highlighter styles the negative-flag prefix separately —
            # ANSI codes land INSIDE "--no-chat". Assert on stripped text.
            plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
            assert "--no-chat" in plain


class TestStatusAndLogs:
    def test_status_lists_runs(self, workdir: Path) -> None:
        seed_store(workdir)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "rseeded11" in result.output
        assert "completed" in result.output

    def test_status_run_detail(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        seed_store(workdir)
        result = runner.invoke(app, ["status", "rseeded11"])
        assert result.exit_code == 0
        assert "Task one" in result.output
        # the pair names print with liveness, so no by-hand reconstruction
        assert "sbxloop-rseeded11-agent" in result.output
        assert "sbxloop-rseeded11-github" in result.output
        assert "not running" in result.output
        assert "sbxloop shell" not in result.output

    def test_status_run_detail_flags_live_sandboxes(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        from sbxloop.sbx.cli import SbxCLI
        from sbxloop.sbx.models import SandboxSpec

        seed_store(workdir)
        SbxCLI(binary=str(fake_sbx.binary)).create(
            SandboxSpec(name="sbxloop-rseeded11-agent", role="agent", workspace=workdir)
        )
        result = runner.invoke(app, ["status", "rseeded11"])
        assert result.exit_code == 0
        assert "running" in result.output
        assert "sbxloop shell rseeded11" in result.output

    def test_status_run_detail_survives_sbx_failure(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        seed_store(workdir)
        fake_sbx.fail_next("ls")
        result = runner.invoke(app, ["status", "rseeded11"])
        assert result.exit_code == 0
        assert "liveness unknown" in result.output

    def test_status_unknown_run(self, workdir: Path) -> None:
        seed_store(workdir)
        result = runner.invoke(app, ["status", "rghost"])
        assert result.exit_code == 2

    def test_status_lists_reconciliation_reason(self, workdir: Path) -> None:
        """#374: a terminal run's persisted reason shows next to its state,
        and a run without one renders no stray separator."""
        store = seed_store(workdir)
        store.create_run("rorphan01", "something abandoned")
        store.reconcile_run("rorphan01", "cancelled", "work item cancelled")
        result = runner.invoke(app, ["status"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "work item cancelled" in plain
        # the run with reason=None keeps a bare state cell
        assert "completed —" not in plain

    def test_status_run_detail_shows_reason(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        store = seed_store(workdir)
        store.create_run("rorphan02", "something abandoned")
        store.reconcile_run(
            "rorphan02", "failed", "orphaned: daemon restarted while run was in flight"
        )
        result = runner.invoke(app, ["status", "rorphan02"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "reason: orphaned: daemon restarted while run was in flight" in plain

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

    def test_logs_follow_exits_on_stale_run(self, workdir: Path) -> None:
        # A run whose driving process died hard stays `building` in the DB
        # forever; --follow must notice the silence and exit, not spin.
        store = seed_store(workdir)
        store.set_run_state("rseeded11", "building")
        store._conn.execute(  # backdate the state change (no public setter)
            "UPDATE runs SET updated_at = 1.0 WHERE run_id = 'rseeded11'"
        )
        store._conn.commit()
        result = runner.invoke(app, ["logs", "rseeded11", "--follow"])
        assert result.exit_code == 0
        # single words: rich may wrap the note anywhere between words
        assert "activity" in result.output
        assert "resume" in result.output


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

    def test_dot_path_artifacts_listed_and_exclusions_noted(self, workdir: Path) -> None:
        """.github/ and .gitignore show up; .git is excluded visibly (#67)."""
        workspace = self.seed_with_workspace(workdir, mounted=True)
        (workspace / ".github" / "workflows").mkdir(parents=True)
        (workspace / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        (workspace / ".gitignore").write_text("*.pyc\n")
        (workspace / ".git").mkdir()
        (workspace / ".git" / "HEAD").write_text("ref\n")
        result = runner.invoke(app, ["artifacts", "rseeded11"])
        assert result.exit_code == 0
        assert "2 file(s)" in result.output
        assert "ci.yml" in result.output
        assert ".gitignore" in result.output
        assert "1 file(s) excluded (.git)" in result.output

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

    def test_config_policy_defaults(self, workdir: Path) -> None:
        result = runner.invoke(app, ["config", "policy"])
        assert result.exit_code == 0
        assert "build" in result.output
        assert "task-declared grants" in result.output
        assert "empty" in result.output  # no [policy] allow configured
        assert "api.githubcopilot.com" in result.output

    def test_config_policy_shows_bounds(self, workdir: Path) -> None:
        (workdir / "sbxloop.toml").write_text(
            '[policy]\nallow = ["registry.npmjs.org"]\ndeny = ["evil.example.com"]\n'
        )
        result = runner.invoke(app, ["config", "policy"])
        assert result.exit_code == 0
        assert "registry.npmjs.org" in result.output
        assert "evil.example.com" in result.output

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

    def test_init_template_documents_landing_knobs(self, workdir: Path) -> None:
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        text = (workdir / "sbxloop.toml").read_text()
        # landing is always on; its budgets and the merge style are documented
        assert "[landing]" in text
        assert "max_review_rounds" in text and "merge_method" in text
        # ...and the daemon's label for a PR the loop could not land
        assert "blocked_label" in text
        # the retired tracker knobs must not be taught to a fresh install
        assert "close_on_success" not in text and "tracking_issue" not in text

        from sbxloop.config import load_config

        config = load_config(cwd=workdir, env={})
        assert config.landing.max_review_rounds == 3
        assert config.daemon.blocked_label == "sbxloop:blocked"
        # the concierge block documents its knobs and stays commented (defaults)
        assert "[concierge]" in text and "session_turns" in text
        assert config.concierge.enabled is True and config.concierge.model is None


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


class TestShellCommand:
    def seed_run_with_sandbox(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        from sbxloop.sbx.cli import SbxCLI
        from sbxloop.sbx.models import SandboxSpec

        seed_store(workdir)
        SbxCLI(binary=str(fake_sbx.binary)).create(
            SandboxSpec(name="sbxloop-rseeded11-agent", role="agent", workspace=workdir)
        )

    def test_unknown_run_errors(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        result = runner.invoke(app, ["shell", "rghost"])
        assert result.exit_code == 2
        assert "unknown run" in result.output

    def test_invalid_role_errors(self, workdir: Path) -> None:
        result = runner.invoke(app, ["shell", "rseeded11", "--role", "bogus"])
        assert result.exit_code == 2
        assert "agent or github" in result.output

    def test_missing_sandbox_errors_with_keep_hint(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        seed_store(workdir)
        result = runner.invoke(app, ["shell", "rseeded11"])
        assert result.exit_code == 2
        assert "not running" in result.output
        assert "keep_on_failure" in result.output

    def test_command_runs_inside_the_sandbox(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        self.seed_run_with_sandbox(workdir, fake_sbx)
        result = runner.invoke(app, ["shell", "rseeded11", "-c", "touch /home/agent/proof"])
        assert result.exit_code == 0, result.output
        assert (fake_sbx.sandbox_fs("sbxloop-rseeded11-agent") / "home/agent/proof").is_file()

    def test_inner_exit_code_passes_through(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        self.seed_run_with_sandbox(workdir, fake_sbx)
        result = runner.invoke(app, ["shell", "rseeded11", "-c", "exit 7"])
        assert result.exit_code == 7


class TestDaemonCommand:
    @staticmethod
    def offline(monkeypatch: pytest.MonkeyPatch, items: list[WorkItem] | None = None) -> None:
        """No sandbox, no GitHub: the stale-sandbox sweep is a no-op and the
        issue poll answers from memory, so `daemon --repo o/r --once` runs a
        whole tick without a network (the test_daemon_control pattern)."""
        from sbxloop.daemon.github import DaemonGithub
        from sbxloop.daemon.sources import GitHubIssueSource

        monkeypatch.setattr(DaemonGithub, "remove_stale", lambda self: None)
        monkeypatch.setattr(GitHubIssueSource, "poll", lambda self: list(items or []))

    def test_no_repository_exits_2(self, workdir: Path) -> None:
        # the daemon's work is the labeled issues of ONE repository
        result = runner.invoke(app, ["daemon"])
        assert result.exit_code == 2
        assert "daemon.no_repository" in result.output

    def test_nonpositive_poll_interval_exits_2(self, workdir: Path) -> None:
        # review: Event.wait(<= 0) returns immediately → the loop would spin
        for value in ("0", "-5"):
            result = runner.invoke(
                app, ["daemon", "--repo", "o/r", "--poll-interval", value, "--once"]
            )
            assert result.exit_code == 2, result.output
            assert "daemon.invalid_option" in result.output

    def test_dry_run_lists_candidates_without_claiming(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.daemon.sources import GitHubIssueSource

        item = WorkItem(
            item_id="gh:issue:12",
            source_key="12",
            title="Do the thing",
            url="https://github.com/o/r/issues/12",
        )
        self.offline(monkeypatch, [item])
        claimed: list[WorkItem] = []

        def claim(self: GitHubIssueSource, item: WorkItem) -> bool:
            claimed.append(item)
            return True

        monkeypatch.setattr(GitHubIssueSource, "claim", claim)
        result = runner.invoke(app, ["daemon", "--repo", "o/r", "--dry-run"])
        assert result.exit_code == 0, result.output
        # the listing is stdout output (pipeable), not only a log line
        assert "gh:issue:12" in result.stdout and "Do the thing" in result.stdout
        assert "issues/12" in result.stdout
        assert claimed == []  # the label swap never happens on a dry run

    def test_discord_configured_without_token_exits_2(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        self.offline(monkeypatch)
        result = runner.invoke(
            app, ["daemon", "--repo", "o/r", "--discord-channel", "123", "--once"]
        )
        assert result.exit_code == 2
        assert "DISCORD_BOT_TOKEN" in result.output

    def test_once_runs_a_tick_and_exits(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.offline(monkeypatch)
        result = runner.invoke(app, ["daemon", "--repo", "o/r", "--once"])
        assert result.exit_code == 0, result.output
        assert "tick:" in result.output and "no_work" in result.output
        assert "daemon.tick" in result.output  # and the structured record

    def test_once_never_starts_the_version_check(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The drift check reaches PyPI, so it belongs to a long-running
        daemon only — this is what keeps the unit suite off the network."""
        from sbxloop.cli import app as app_mod

        started: list[object] = []
        # Patch where it is USED: app binds the name at import time, so
        # patching sbxloop.daemon.versions would pass no matter what.
        monkeypatch.setattr(app_mod, "start_drift_check", lambda *a, **k: started.append(a))
        self.offline(monkeypatch)
        result = runner.invoke(app, ["daemon", "--repo", "o/r", "--once"])
        assert result.exit_code == 0, result.output
        assert started == []

    def test_state_dir_defaults_outside_cwd_and_is_announced(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#255: daemon state is anchored to XDG state home, not a relative
        .sbxloop that would nest run clones inside the workspace checkout."""
        self.offline(monkeypatch)
        result = runner.invoke(app, ["daemon", "--repo", "o/r", "--once"])
        assert result.exit_code == 0, result.output
        expected = (workdir / "xdg-state" / "sbxloop" / workdir.name).resolve()
        assert (expected / "state.db").is_file()
        assert not (workdir / ".sbxloop" / "state.db").exists()
        assert "daemon.starting" in result.output
        assert str(expected) in result.output.replace("\n", "")

    def test_startup_summary_names_the_configuration(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.offline(monkeypatch)
        result = runner.invoke(
            app, ["daemon", "--repo", "o/r", "--once", "--max-runs-per-day", "7"]
        )
        assert result.exit_code == 0, result.output
        (line,) = [ln for ln in result.output.splitlines() if "daemon.starting" in ln]
        for field in (
            "repo=o/r",
            "trigger_label=",
            "max_runs_per_day=7",
            "poll_interval_s=",
            "landing=on",
            "max_review_rounds=3",
            "max_ci_rounds=",
            "merge_method=",
            "chat=off",
            "log_level=INFO",
            "state_dir_reason=",
        ):
            assert field in line, line
        # and the run tick + orderly shutdown follow it
        assert "daemon.tick" in result.output and "daemon.stopped" in result.output

    def test_max_runs_per_day_help_names_the_calendar_day(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag gates a calendar day, so its help must not read as a
        trailing window (operators read ``10/10`` and expected no more runs)."""
        # Wide terminal + ANSI stripping, as elsewhere: rich wraps the 80-col
        # panel mid-token and styles "--" apart from the flag name, so
        # "--max-runs-per-day" is never contiguous in the raw output.
        monkeypatch.setenv("COLUMNS", "300")
        plain = re.sub(r"\x1b\[[0-9;]*m", "", runner.invoke(app, ["daemon", "--help"]).output)
        (help_text,) = [line for line in plain.splitlines() if "--max-runs-per-day" in line]
        assert "Calendar-day" in help_text and "run_cap_timezone" in help_text
        assert "24h" not in help_text and "olling" not in help_text

    def test_log_level_flag_beats_env(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.offline(monkeypatch)
        monkeypatch.setenv("SBXLOOP_DAEMON__LOG_LEVEL", "WARNING")
        quiet = runner.invoke(app, ["daemon", "--repo", "o/r", "--once"])
        assert quiet.exit_code == 0, quiet.output
        assert "daemon.starting" not in quiet.output  # INFO suppressed by env
        loud = runner.invoke(app, ["daemon", "--repo", "o/r", "--once", "--log-level", "debug"])
        assert loud.exit_code == 0, loud.output
        assert "daemon.starting" in loud.output and "log_level=DEBUG" in loud.output
        assert "store.opened" in loud.output  # a DEBUG-only line

    def test_log_format_json(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.offline(monkeypatch)
        result = runner.invoke(app, ["daemon", "--repo", "o/r", "--once", "--log-format", "json"])
        assert result.exit_code == 0, result.output
        objects = [json.loads(ln) for ln in result.output.splitlines() if ln.startswith("{")]
        events = [o["event"] for o in objects]
        assert "daemon.starting" in events and "daemon.tick" in events
        tick = next(o for o in objects if o["event"] == "daemon.tick")
        assert tick["idle"] == "no_work" and tick["level"] == "info"

    def test_bad_log_level_exits_2(self, workdir: Path) -> None:
        result = runner.invoke(app, ["daemon", "--repo", "o/r", "--log-level", "loud"])
        assert result.exit_code == 2
        assert "daemon.invalid_option" in result.output

    def test_legacy_state_dir_keeps_being_used(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_store(workdir)  # an existing ./.sbxloop/state.db from before the change
        self.offline(monkeypatch)
        result = runner.invoke(app, ["daemon", "--repo", "o/r", "--once"])
        assert result.exit_code == 0, result.output
        assert "legacy" in result.output
        assert not (workdir / "xdg-state").exists()

    def test_explicit_daemon_state_dir_wins(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SBXLOOP_DAEMON__STATE_DIR", str(workdir / "elsewhere"))
        self.offline(monkeypatch)
        result = runner.invoke(app, ["daemon", "--repo", "o/r", "--once"])
        assert result.exit_code == 0, result.output
        assert (workdir / "elsewhere" / "state.db").is_file()

    def test_legacy_state_db_is_archived_on_start(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-1.0 daemon database (item kinds, no schema version) is moved
        aside on the first start rather than migrated; the daemon begins
        with a fresh store next to it and says so in the journal."""
        state = workdir / "xdg-state" / "sbxloop" / workdir.name
        state.mkdir(parents=True)
        conn = sqlite3.connect(state / "state.db")
        conn.execute("CREATE TABLE daemon_work_items (item_id TEXT PRIMARY KEY, kind TEXT)")
        conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO daemon_work_items (item_id, kind) VALUES ('inbox:x.md', 'inbox')")
        conn.commit()
        conn.close()
        self.offline(monkeypatch)
        result = runner.invoke(app, ["daemon", "--repo", "o/r", "--once"])
        assert result.exit_code == 0, result.output
        assert "store.archived_legacy" in result.output
        assert (state / "state.db.pre-1.0").is_file()
        assert (state / "state.db").is_file()
        assert "daemon.tick" in result.output
        # the fresh store knows nothing of the old lanes' items
        listed = runner.invoke(app, ["daemon", "items"])
        assert listed.exit_code == 0, listed.output
        assert "no work items" in listed.output


class TestDaemonItemControls:
    """#229: `sbxloop daemon items|abandon|retry|requeue` act on the store
    the daemon shares; they need no live daemon and no sandbox."""

    @staticmethod
    def daemon_state(workdir: Path) -> Path:
        # Where the daemon itself keeps its queue — the anchored XDG default
        # (#255), not the runner dir's `.sbxloop`; seeding there proves the
        # item controls follow the daemon's state-dir rule.
        assert not (workdir / ".sbxloop" / "state.db").exists()
        return workdir / "xdg-state" / "sbxloop" / workdir.name

    def seed(self, workdir: Path) -> None:
        from sbxloop.daemon.store import DaemonStore

        dstore = DaemonStore(self.daemon_state(workdir) / "state.db")
        dstore.upsert_new(WorkItem(item_id="gh:issue:12", source_key="12", title="Do X"), 1.0)
        dstore.mark_running("gh:issue:12", "r_x", 2.0)
        dstore.close()

    def test_items_lists_state_attempts_and_run(self, workdir: Path) -> None:
        result = runner.invoke(app, ["daemon", "items"])
        assert result.exit_code == 0, result.output
        assert "no work items" in result.output
        self.seed(workdir)
        result = runner.invoke(app, ["daemon", "items"])
        assert result.exit_code == 0, result.output
        assert "gh:issue:12" in result.output and "running" in result.output
        assert "r_x" in result.output
        result = runner.invoke(app, ["daemon", "items", "--state", "queued"])
        assert "no work items" in result.output
        for bogus in ("bogus", "abandoned"):  # `abandoned` is not a state any more
            result = runner.invoke(app, ["daemon", "items", "--state", bogus])
            assert result.exit_code == 2 and "unknown item state" in result.output

    def test_abandon_retry_requeue_transitions(self, workdir: Path) -> None:
        from sbxloop.daemon.store import DaemonStore

        self.seed(workdir)
        result = runner.invoke(app, ["daemon", "retry", "gh:issue:12"])
        assert result.exit_code == 2 and "retry refused" in result.output
        result = runner.invoke(
            app, ["daemon", "abandon", "gh:issue:12", "--reason", "plan spiraled"]
        )
        assert result.exit_code == 0, result.output
        # FORCE_COLOR / a forced terminal makes rich highlight the number
        # ("attempts \x1b[1;36m1"): assert on the ANSI-stripped text.
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        # an operator abandon is a failure by decision; the run stays pinned
        # so the ledger and `sbxloop logs` still tie the item to it
        assert "gh:issue:12: failed (attempts 1, run r_x)" in plain
        dstore = DaemonStore(self.daemon_state(workdir) / "state.db")
        item = dstore.get("gh:issue:12")
        assert item is not None and item.state == "failed"
        assert item.last_error == "plan spiraled" and item.run_id == "r_x"
        assert item.pending_report == "abandoned"  # the issue is owed the news
        dstore.close()
        result = runner.invoke(app, ["daemon", "requeue", "gh:issue:12"])
        assert result.exit_code == 2 and "requeue refused" in result.output
        assert "use retry" in result.output
        result = runner.invoke(app, ["daemon", "retry", "gh:issue:12"])
        assert result.exit_code == 0, result.output
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "gh:issue:12: queued (attempts 0)" in plain
        dstore = DaemonStore(self.daemon_state(workdir) / "state.db")
        item = dstore.get("gh:issue:12")
        assert item is not None and item.run_id is None  # a fresh run, not a resume
        assert item.pending_report == "requeued"
        dstore.close()
        result = runner.invoke(app, ["daemon", "abandon", "gh:issue:404"])
        assert result.exit_code == 2 and "unknown work item" in result.output

    def test_item_verbs_take_legacy_and_typed_ids(self, workdir: Path) -> None:
        """#508: `gh:12` is the legacy spelling of `gh:issue:12`. Both forms
        must reach the same row, and every rendering is the typed one."""
        from sbxloop.daemon.store import DaemonStore

        self.seed(workdir)
        result = runner.invoke(app, ["daemon", "requeue", "gh:12"])  # legacy in
        assert result.exit_code == 0, result.output
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "gh:issue:12: queued" in plain
        assert not re.search(r"gh:\d", plain)
        result = runner.invoke(app, ["daemon", "abandon", "gh:12", "--reason", "enough"])
        assert result.exit_code == 0, result.output
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "gh:issue:12: failed" in plain and not re.search(r"gh:\d", plain)
        result = runner.invoke(app, ["daemon", "retry", "gh:issue:12"])  # typed in
        assert result.exit_code == 0, result.output
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "gh:issue:12: queued (attempts 0)" in plain
        dstore = DaemonStore(self.daemon_state(workdir) / "state.db")
        item = dstore.get("gh:12")
        assert item is not None and item.item_id == "gh:issue:12"
        dstore.close()

    def test_items_listing_renders_typed_ids_for_legacy_rows(self, workdir: Path) -> None:
        """A row written by the pre-#508 daemon still lists — as `gh:issue:`."""
        import sqlite3

        from sbxloop.daemon.store import DaemonStore

        self.seed(workdir)
        db = self.daemon_state(workdir) / "state.db"
        conn = sqlite3.connect(db)
        conn.execute("UPDATE daemon_work_items SET item_id = 'gh:12'")
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["daemon", "items"])
        assert result.exit_code == 0, result.output
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "gh:issue:12" in plain and not re.search(r"gh:\d", plain)
        dstore = DaemonStore(db)
        assert dstore.get("gh:issue:12") is not None
        dstore.close()

    def test_item_ids_in_help_are_typed(self, workdir: Path) -> None:
        for word in ("abandon", "retry", "requeue"):
            out = runner.invoke(app, ["daemon", word, "--help"]).output
            assert "gh:issue:12" in out and not re.search(r"gh:\d", out)

    def test_requeue_unpins_a_running_item(self, workdir: Path) -> None:
        from sbxloop.daemon.store import DaemonStore

        self.seed(workdir)
        result = runner.invoke(app, ["daemon", "requeue", "gh:issue:12"])
        assert result.exit_code == 0, result.output
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "gh:issue:12: queued (attempts 1)" in plain  # attempts kept, run gone
        dstore = DaemonStore(self.daemon_state(workdir) / "state.db")
        item = dstore.get("gh:issue:12")
        assert item is not None and item.state == "queued" and item.run_id is None
        dstore.close()

    def test_daemon_help_lists_subcommands_and_options(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COLUMNS", "300")
        result = runner.invoke(app, ["daemon", "--help"])
        assert result.exit_code == 0
        # GitHub Actions forces typer's help colours on and the option
        # highlighter styles "--" and "repo" separately, so "--repo" is
        # never a contiguous substring of the raw output.
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        for word in ("--repo", "--once", "--dry-run", "items", "abandon", "retry", "requeue"):
            assert word in plain
        # the inbox and backlog lanes are gone: one labeled issue → one run
        assert "--inbox" not in plain and "--backlog" not in plain


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

    def test_doctor_reports_the_concierge_when_discord_is_on(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.cli.doctor import collect_checks

        (workdir / "sbxloop.toml").write_text("[discord]\nchannel_id = 42\n")
        env = {"GH_TOKEN": "tok", "DISCORD_BOT_TOKEN": "tok"}
        (row,) = [c for c in collect_checks(env) if c.name == "chat concierge"]
        assert not row.ok and not row.hard and "COPILOT_GITHUB_TOKEN not set" in row.detail
        env["COPILOT_GITHUB_TOKEN"] = "tok"
        (row,) = [c for c in collect_checks(env) if c.name == "chat concierge"]
        assert row.ok and "180s per message" in row.detail
        (workdir / "sbxloop.toml").write_text(
            "[discord]\nchannel_id = 42\n[concierge]\nenabled = false\n"
        )
        assert not [c for c in collect_checks(env) if c.name == "chat concierge"]

    def test_doctor_hints_at_legacy_relative_state_dir(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#224: a ``./.sbxloop`` from the former relative default is
        silently ignored once state_dir defaults to ``~/.sbxloop``; doctor
        must say so (soft) — unless the operator opted in explicitly."""
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GH_TOKEN", "tok")
        monkeypatch.setenv("HOME", str(workdir / "home"))
        (workdir / ".sbxloop").mkdir()
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output  # warn, not FAIL
        assert "legacy state dir" in result.output
        # rich folds the detail column, so check the remedy text unwrapped
        from sbxloop.cli.doctor import collect_checks

        (legacy,) = [c for c in collect_checks(dict(os.environ)) if c.name == "legacy state dir"]
        assert not legacy.ok and not legacy.hard
        assert 'state_dir = ".sbxloop"' in legacy.detail

        (workdir / "sbxloop.toml").write_text('state_dir = ".sbxloop"\n')
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "legacy state dir" not in result.output

    def test_doctor_no_legacy_hint_when_default_is_here(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # HOME == cwd (autouse fixture): ./.sbxloop *is* the default state dir.
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GH_TOKEN", "tok")
        (workdir / ".sbxloop").mkdir()
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "legacy state dir" not in result.output

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

    def _bake_record(
        self, workdir: Path, *, worker_version: str, ref: str, git: bool | None = None
    ) -> None:
        state = workdir / ".sbxloop"
        state.mkdir(exist_ok=True)
        record: dict[str, object] = {
            "ref": ref,
            "worker_version": worker_version,
            "python": "/home/agent/.sbxloop/venv/bin/python",
            "runtime_cached": True,
            "baked_at": 0.0,
        }
        if git is not None:
            record["git"] = git
        (state / "bake.json").write_text(json.dumps(record))

    def _git_row(self, workdir: Path, fake_sbx: FakeSbx, git: bool | None) -> Check:
        from sbxloop.cli.doctor import collect_checks
        from sbxloop.sbx.cli import SbxCLI

        self._bake_record(
            workdir, worker_version=sbxloop.__version__, ref="sbxloop-baked:latest", git=git
        )
        checks = collect_checks(
            {"COPILOT_GITHUB_TOKEN": "tok", "SBXLOOP_SANDBOX__TEMPLATE": "sbxloop-baked:latest"},
            cli=SbxCLI(binary=str(fake_sbx.binary)),
        )
        return {c.name: c for c in checks}["git in template"]

    def test_doctor_git_in_template_ok_when_baked_with_git(
        self, workdir: Path, fake_sbx: FakeSbx
    ) -> None:
        # #252: git is baseline agent tooling; a bake that captured it means
        # no per-run apt top-up.
        row = self._git_row(workdir, fake_sbx, git=True)
        assert row.ok and "git on PATH" in row.detail

    def test_doctor_git_in_template_missing_is_soft_warn(
        self, workdir: Path, fake_sbx: FakeSbx
    ) -> None:
        # Missing git is a warn, not a FAIL: provisioning still probes and
        # apt-installs it per run, so the run is not lost — only slower.
        row = self._git_row(workdir, fake_sbx, git=False)
        assert not row.ok and not row.hard
        assert "sbxloop bake" in row.detail

    def test_doctor_git_in_template_unrecorded_by_older_bake(
        self, workdir: Path, fake_sbx: FakeSbx
    ) -> None:
        # Records written before the field existed still load; the row says
        # "not recorded" rather than inventing a verdict.
        row = self._git_row(workdir, fake_sbx, git=None)
        assert row.ok and not row.hard
        assert "not recorded" in row.detail

    def test_doctor_template_fresh_and_listed(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rendered table wraps long details, so assert on the checks."""
        from sbxloop.cli.doctor import collect_checks
        from sbxloop.sbx.cli import SbxCLI

        self._bake_record(workdir, worker_version=sbxloop.__version__, ref="sbxloop-baked:latest")
        fake_sbx.script("template ls", stdout="REPOSITORY  TAG\nsbxloop-baked  latest\n")
        checks = collect_checks(
            {"COPILOT_GITHUB_TOKEN": "tok", "SBXLOOP_SANDBOX__TEMPLATE": "sbxloop-baked:latest"},
            cli=SbxCLI(binary=str(fake_sbx.binary)),
        )
        by_name = {c.name: c for c in checks}
        template = by_name["sandbox template"]
        assert template.ok and "baked with worker" in template.detail
        available = by_name["template available"]
        assert available.ok and "listed" in available.detail

    def test_doctor_stale_template_warns_rebake(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("SBXLOOP_SANDBOX__TEMPLATE", "sbxloop-baked:latest")
        self._bake_record(workdir, worker_version="0.0.0", ref="sbxloop-baked:latest")
        result = runner.invoke(app, ["doctor"])
        # stale template is a warning (runs fall back to the ladder), never a FAIL
        assert result.exit_code == 0, result.output
        assert "stale" in result.output
        assert "sbxloop bake" in result.output

    def test_doctor_unbaked_template_is_soft(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.cli.doctor import collect_checks
        from sbxloop.sbx.cli import SbxCLI

        checks = collect_checks(
            {
                "COPILOT_GITHUB_TOKEN": "tok",
                "SBXLOOP_SANDBOX__TEMPLATE": "docker.io/you/custom:v1",
            },
            cli=SbxCLI(binary=str(fake_sbx.binary)),
        )
        by_name = {c.name: c for c in checks}
        template = by_name["sandbox template"]
        assert template.ok and not template.hard
        assert "not baked on this host" in template.detail
        # not in `sbx template ls` either -> soft warn with remediation
        available = by_name["template available"]
        assert not available.ok and not available.hard
        assert "sbxloop bake" in available.detail

    def test_doctor_no_template_no_template_checks(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "sandbox template" not in result.output

    def _sdk_kind_check(self, fake_sbx: FakeSbx):
        from sbxloop.cli.doctor import collect_checks
        from sbxloop.sbx.cli import SbxCLI

        checks = collect_checks(
            {"COPILOT_GITHUB_TOKEN": "tok"}, cli=SbxCLI(binary=str(fake_sbx.binary))
        )
        return {c.name: c for c in checks}["copilot sdk permission kinds"]

    def test_doctor_sdk_kinds_soft_ok_when_sdk_absent(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.cli import doctor

        monkeypatch.setattr(doctor, "installed_sdk_permission_kinds", lambda: None)
        check = self._sdk_kind_check(fake_sbx)
        assert check.ok and not check.hard
        assert "not installed" in check.detail
        assert "fails closed" in check.detail

    def test_doctor_sdk_kinds_match_verified_vocabulary(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.cli import doctor

        monkeypatch.setattr(
            doctor, "installed_sdk_permission_kinds", lambda: doctor.SDK_PERMISSION_KINDS
        )
        check = self._sdk_kind_check(fake_sbx)
        assert check.ok
        assert "matches the verified vocabulary" in check.detail

    def test_doctor_sdk_kind_drift_warns_naming_the_kinds(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.cli import doctor

        drifted = (doctor.SDK_PERMISSION_KINDS - {"read"}) | {"novel-kind"}
        monkeypatch.setattr(doctor, "installed_sdk_permission_kinds", lambda: drifted)
        check = self._sdk_kind_check(fake_sbx)
        # drift is a loud warning, never a FAIL: the barrier fails closed
        assert not check.ok and not check.hard
        assert "novel-kind" in check.detail
        assert "read" in check.detail

    def test_doctor_without_sbx(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", str(workdir))  # nothing on PATH
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GH_TOKEN", "tok")
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "not found on PATH" in result.output
        assert "conformance skipped" in result.output

    def test_doctor_shows_conformance_with_deep_hint(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "sbx conformance" in result.output
        # rich may wrap the hint, so match the flag token alone
        assert "--deep" in result.output
        assert "unprobed" in result.output
        # cheap probes never create a sandbox
        assert not (fake_sbx.state / "sandboxes").is_dir() or not list(
            (fake_sbx.state / "sandboxes").iterdir()
        )

    def test_doctor_deep_probes_and_caches(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.sbx.conformance import CATALOG, load_verdicts

        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        result = runner.invoke(app, ["doctor", "--deep"])
        assert result.exit_code == 0, result.output
        assert "DRIFT" not in result.output
        cached = load_verdicts(workdir / ".sbxloop", "0.38.0")
        assert set(cached) == {probe.id for probe in CATALOG}
        # the scratch sandbox is gone afterwards
        assert not list((fake_sbx.state / "sandboxes").iterdir())
        # and a follow-up shallow doctor is fully probed: no more deep nudge
        again = runner.invoke(app, ["doctor"])
        assert "unprobed" not in again.output

    def test_doctor_alarms_on_cached_drift(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time as time_module

        from sbxloop.sbx.conformance import (
            PROBE_WORKSPACE_MOUNT,
            ProbeRecord,
            save_verdicts,
        )

        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        # A probe that still carries an `expected`; secret-env-visibility now
        # carries None, since provisioning auto-heals every answer.
        save_verdicts(
            workdir / ".sbxloop",
            "0.38.0",
            {
                PROBE_WORKSPACE_MOUNT: ProbeRecord(
                    verdict="harvest-only", checked_at=time_module.time()
                )
            },
        )
        result = runner.invoke(app, ["doctor"])
        assert "sbx drift" in result.output
        # drift warns loudly but does not fail an otherwise-ready host
        assert result.exit_code == 0, result.output
        # ...unless the caller asked for the CI gate (#226)
        gated = runner.invoke(app, ["doctor", "--fail-on-drift"])
        assert gated.exit_code == 1, gated.output
        assert "conformance gate failed" in gated.output

    def test_doctor_fail_on_drift_rejects_unprobed_sbx_version(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A never-deep-probed sbx build is not a passing grade for the gate:
        that is exactly the state a fresh sbx release lands in."""
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        result = runner.invoke(app, ["doctor", "--fail-on-drift"])
        assert result.exit_code == 1, result.output
        assert "conformance gate failed" in result.output
        # a deep run answers every probe against the fake and the gate opens
        deep = runner.invoke(app, ["doctor", "--deep", "--fail-on-drift"])
        assert deep.exit_code == 0, deep.output
        assert runner.invoke(app, ["doctor", "--fail-on-drift"]).exit_code == 0


class TestBakeCommand:
    """CLI wiring only — the bake flow itself is covered in test_bake.py."""

    def _stub_record(self, **overrides: Any) -> Any:
        from sbxloop.sbx.bake import BakeRecord

        base: dict[str, Any] = {
            "ref": "sbxloop-baked:latest",
            "worker_version": sbxloop.__version__,
            "python": "/home/agent/.sbxloop/venv/bin/python",
            "runtime_cached": True,
            "baked_at": 0.0,
        }
        base.update(overrides)
        return BakeRecord.model_validate(base)

    def test_bake_success_prints_config_hint(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sbxloop.cli.app as app_mod

        captured: dict[str, Any] = {}

        def fake_bake(cli: Any, config: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return self._stub_record()

        monkeypatch.setattr(app_mod, "bake_template", fake_bake)
        result = runner.invoke(app, ["bake", "--no-runtime-cache", "--keep"])
        assert result.exit_code == 0, result.output
        assert captured["cache_runtime"] is False
        assert captured["keep"] is True
        assert captured["ref"] == "sbxloop-baked:latest"
        assert 'template = "sbxloop-baked:latest"' in result.output

    def test_bake_notes_already_configured_template(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sbxloop.cli.app as app_mod

        monkeypatch.setenv("SBXLOOP_SANDBOX__TEMPLATE", "sbxloop-baked:latest")
        monkeypatch.setattr(app_mod, "bake_template", lambda *a, **k: self._stub_record())
        result = runner.invoke(app, ["bake"])
        assert result.exit_code == 0, result.output
        assert "already points at this ref" in result.output

    def test_bake_failure_exits_2(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import sbxloop.cli.app as app_mod
        from sbxloop.errors import BakeError

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise BakeError("bake failed: sandbox exploded")

        monkeypatch.setattr(app_mod, "bake_template", fail)
        result = runner.invoke(app, ["bake"])
        assert result.exit_code == 2
        assert "bake failed" in result.output


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
                {"text": "did it"},
            ],
        )
        result = runner.invoke(app, ["run", "make it so", "--no-tui"])
        assert result.exit_code == 0, result.output
        assert "finished" in result.output
        assert "completed" in result.output
        assert "t1: done" in result.output

    HAPPY_RUN: ClassVar[list[dict[str, Any]]] = [
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
        {"text": "did it"},
    ]

    def test_run_no_keep_sandboxes_overrides_config(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # keep_sandboxes=true in config must be forceable OFF from the CLI.
        self.make_run_env(workdir, monkeypatch, self.HAPPY_RUN)
        monkeypatch.setenv("SBXLOOP_KEEP_SANDBOXES", "true")
        result = runner.invoke(app, ["run", "make it so", "--no-tui", "--no-keep-sandboxes"])
        assert result.exit_code == 0, result.output
        boxes = fake_sbx.state / "sandboxes"
        assert not boxes.is_dir() or not any(boxes.iterdir())

    def test_run_keep_sandboxes_flag_keeps(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.make_run_env(workdir, monkeypatch, self.HAPPY_RUN)
        result = runner.invoke(app, ["run", "make it so", "--no-tui", "--keep-sandboxes"])
        assert result.exit_code == 0, result.output
        boxes = fake_sbx.state / "sandboxes"
        assert any(p.name.startswith("sbxloop-") for p in boxes.iterdir())

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
                {
                    "text": "did it",
                    "events": [{"type": "agent.message", "data": {"content": m}} for m in messages],
                },
            ],
        )
        result = runner.invoke(app, ["run", "make it so"])  # tui mode (default)
        assert result.exit_code == 0, result.output
        for message in messages:
            assert message in result.output

    def test_run_no_tui_ctrl_c_removes_sandboxes_and_hints_resume(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ctrl+C mid-run exits 130 without a traceback, removes the run's
        sandboxes, and points at `sbxloop resume` (the run state stays
        resumable)."""
        self.make_run_env(workdir, monkeypatch, [])
        from sbxloop.engine.phases import PhaseRunner

        def interrupt(self: PhaseRunner) -> Any:
            raise KeyboardInterrupt

        monkeypatch.setattr(PhaseRunner, "decompose", interrupt)
        result = runner.invoke(app, ["run", "make it so", "--no-tui"])
        assert result.exit_code == 130, result.output
        assert "interrupted" in result.output
        assert "resume" in result.output
        assert "Traceback" not in result.output
        removed = fake_sbx.invocations("rm")
        assert any(arg.endswith("-agent") for args in removed for arg in args), removed

    def test_run_tui_ctrl_c_exits_130_without_traceback(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A KeyboardInterrupt in the main display loop (where Ctrl+C lands
        in TUI mode, while the engine runs on a worker thread) exits 130
        cleanly instead of leaking a traceback and a live engine thread."""
        import time as real_time

        import sbxloop.cli.app as app_module

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
                {"text": "did it"},
            ],
        )

        class InterruptingTime:
            """time shim for the drive loop: first sleep is the Ctrl+C."""

            def __getattr__(self, name: str) -> Any:
                return getattr(real_time, name)

            @staticmethod
            def sleep(seconds: float) -> None:
                raise KeyboardInterrupt

        monkeypatch.setattr(app_module, "time", InterruptingTime())
        result = runner.invoke(app, ["run", "make it so"])  # tui mode (default)
        assert result.exit_code == 130, result.output
        assert "interrupted" in result.output
        assert "Traceback" not in result.output

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
                {"text": "did it", "files": {"hello.txt": "hi", "docs/readme.md": "# hi"}},
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

    # A single-task run that lands: the builder writes a file (delivery
    # snapshots the workspace, so there has to be something to deliver) and
    # the run's own review approves the diff first time.
    LANDED_RUN: ClassVar[list[dict[str, Any]]] = [
        HAPPY_RUN[0],
        {"text": "did it", "files": {"hello.txt": "hi"}},
        {"json": {"verdict": "approve", "summary": "looked", "findings": []}},
    ]

    def _delivery_env(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGithub:
        """A scripted single-task run that lands a pull request on a
        FakeGithub. The CLI builds its own engine, so the fake is threaded
        in by wrapping ``LoopEngine`` where app.py binds it and handing it
        through the engine's ``github_ops`` seam. Returns the fake, which
        records everything the run asked GitHub for. The CI-wait knobs are
        set the way an operator would set them (environment)."""
        import sbxloop.cli.app as app_mod
        from sbxloop.engine.engine import LoopEngine

        fake = FakeGithub(repo="o/r", number=8, draft=True)

        def engine_with_fake(config: Any, **kwargs: Any) -> LoopEngine:
            return LoopEngine(config, github_ops=lambda client, run_id: fake, **kwargs)

        monkeypatch.setattr(app_mod, "LoopEngine", engine_with_fake)
        self.make_run_env(workdir, monkeypatch, self.LANDED_RUN)
        monkeypatch.setenv("SBXLOOP_LANDING__CI_POLL_INTERVAL_S", "0.01")
        monkeypatch.setenv("SBXLOOP_LANDING__CI_SETTLE_S", "0")
        return fake

    def test_run_with_repo_lands_a_pull_request(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a repository configured, `sbxloop run` is the whole pipeline:
        a draft PR, the run's own review, CI, the merge. The finish summary
        restates the GitHub outcome — it must not live only in scrollback."""
        fake = self._delivery_env(workdir, monkeypatch)
        monkeypatch.setenv("SBXLOOP_GITHUB__REPO", "o/r")
        result = runner.invoke(app, ["run", "ship it", "--no-tui"])
        assert result.exit_code == 0, result.output
        assert len(fake.merges) == 1
        assert fake.pr["merged"] is True
        assert "finished: merged" in result.output
        assert "github: o/r" in result.output
        assert "PR #8" in result.output and "pull/8" in result.output
        assert "review round 1: approve" in result.output
        assert "merged by sbxloop" in result.output

    def test_run_repo_flag_enables_github_without_config(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--repo alone turns the GitHub integration on — no sbxloop.toml
        needed — and the run lands there."""
        fake = self._delivery_env(workdir, monkeypatch)
        result = runner.invoke(app, ["run", "ship it", "--no-tui", "--repo", "o/cli"])
        assert result.exit_code == 0, result.output
        assert len(fake.merges) == 1
        assert "finished: merged" in result.output
        assert "github: o/cli" in result.output

    def test_run_repo_flag_overrides_configured_repo(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = self._delivery_env(workdir, monkeypatch)
        monkeypatch.setenv("SBXLOOP_GITHUB__REPO", "o/toml")
        result = runner.invoke(app, ["run", "ship it", "--no-tui", "--repo", "o/cli"])
        assert result.exit_code == 0, result.output
        assert len(fake.merges) == 1
        assert "github: o/cli" in result.output
        assert "o/toml" not in result.output

    def test_run_create_repo_flags_reach_the_probe(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sbxloop.engine.engine as engine_mod

        self._delivery_env(workdir, monkeypatch)
        seen: dict[str, Any] = {}

        def fake_ensure(ops: Any, repo: str, *, create: bool = False, public: bool = False) -> bool:
            seen.update(repo=repo, create=create, public=public)
            return True

        monkeypatch.setattr(engine_mod, "ensure_repository", fake_ensure)
        result = runner.invoke(
            app,
            [
                "run",
                "ship it",
                "--no-tui",
                "--repo",
                "o/new",
                "--create-repo",
                "--create-public",
            ],
        )
        assert result.exit_code == 0, result.output
        assert seen == {"repo": "o/new", "create": True, "public": True}
        # creation is restated in the finish summary
        assert "github: o/new" in result.output
        assert "created this run" in result.output

    def test_run_deliver_base_is_forwarded_and_pr_opens_as_draft(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = self._delivery_env(workdir, monkeypatch)
        result = runner.invoke(
            app,
            ["run", "ship it", "--no-tui", "--repo", "o/cli", "--deliver-base", "develop"],
        )
        assert result.exit_code == 0, result.output
        assert fake.pr_kwargs["base"] == "develop"
        # [landing] deliver_draft defaults on: the PR opens as a draft and is
        # taken out of draft only once the run has cleared its own bar
        assert fake.pr_kwargs["draft"] is True
        assert fake.ready_calls == ["PR_node8"]
        assert fake.pr["draft"] is False

    def test_run_malformed_repo_flag_refused_up_front(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.make_run_env(workdir, monkeypatch, [])
        result = runner.invoke(app, ["run", "ship it", "--no-tui", "--repo", "not-a-repo"])
        assert result.exit_code == 2
        assert "invalid GitHub option" in result.output
        assert "Traceback" not in result.output
        assert fake_sbx.invocations("create") == []

    def test_run_failure_exit_code(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # decompose fails twice -> WorkerError -> exit 2
        bad = {"json": {"tasks": [{"id": "t1"}]}}
        self.make_run_env(workdir, monkeypatch, [bad, bad])
        result = runner.invoke(app, ["run", "impossible", "--no-tui"])
        assert result.exit_code == 2
        assert "run failed" in result.output
        # default: even a failed run's sandboxes are torn down
        assert (fake_sbx.state / "sandboxes").is_dir() is False or not any(
            (fake_sbx.state / "sandboxes").iterdir()
        )

    def test_run_keep_on_failure_flag_keeps_sandboxes(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = {"json": {"tasks": [{"id": "t1"}]}}
        self.make_run_env(workdir, monkeypatch, [bad, bad])
        result = runner.invoke(app, ["run", "impossible", "--no-tui", "--keep-on-failure"])
        assert result.exit_code == 2
        boxes = fake_sbx.state / "sandboxes"
        assert any(p.name.startswith("sbxloop-") for p in boxes.iterdir())
        # the run.keep event reaches the transcript with the shell pointer
        assert "sbxloop shell" in result.output

    def test_failed_run_summary_prints_kept_hint(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Task fails on budgets (not infra), so the run finishes "failed"
        # and the summary must point at the kept pair.
        execute = {"text": "tried"}
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
                                "verify_commands": ["false"],
                            }
                        ]
                    }
                },
                # 3 builds burn the revisions, then the replan's fresh
                # session burns 3 more — verify ("false") fails them all.
                *[execute] * 6,
            ],
        )
        monkeypatch.setenv("SBXLOOP_KEEP_ON_FAILURE", "true")
        result = runner.invoke(app, ["run", "doomed", "--no-tui"])
        assert result.exit_code == 1, result.output
        assert "sandboxes kept:" in result.output
        assert "sbxloop shell" in result.output
        assert "sandbox rm --run" in result.output


class TestArtifactsTree:
    def test_tree_caps_and_hides_denylisted_dirs(self, tmp_path: Path) -> None:
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
        # denylist, not "anything dot-prefixed": .git excluded, .hidden kept (#67)
        assert ".git" not in {p.parts[-2] for p in files}
        assert len(files) == 7

        console = Console(record=True, width=100)
        console.print(_artifacts_tree(root, files, cap=3))
        text = console.export_text()
        assert ".hidden" in text
        assert "f0.txt" in text
        assert "2.0 KB" in text
        assert "+4 more" in text
        assert ".git" not in text

    def test_human_size_units(self) -> None:
        from sbxloop.cli.app import _human_size

        assert _human_size(3) == "3 B"
        assert _human_size(2048) == "2.0 KB"
        assert _human_size(5 * 1024 * 1024) == "5.0 MB"
        assert _human_size(3 * 1024**3) == "3072.0 MB"


class TestWorkspaceCloneSummary:
    """The finish summary restates where an isolated run's results live,
    mined from the persisted sandbox.workspace_clone event."""

    def _summary(self, tmp_path: Path, *, mounted: bool) -> str:
        import sbxloop.cli.app as app_mod
        from sbxloop.config import Config
        from sbxloop.engine.model import RunResult

        config = Config.model_validate({"state_dir": str(tmp_path / "state")})
        store = StateStore(config.state_dir / "state.db")
        store.create_run("r1", "improve the project")
        store.append_event(
            Event.now(
                "sandbox.workspace_clone",
                "r1",
                source="/home/me/proj",
                target="/state/runs/r1/workspace",
                commit="a" * 40,
                branch="sbxloop/r1",
                dirty=False,
                reused=False,
                message="cloned",
            )
        )
        result = RunResult(run_id="r1", state="completed", mounted=mounted)
        with app_mod.console.capture() as capture:
            app_mod._print_workspace_clone_summary(result, config)
        return capture.get()

    def test_mounted_run_prints_fetch_hint(self, tmp_path: Path) -> None:
        text = self._summary(tmp_path, mounted=True)
        assert "cloned from /home/me/proj" in text
        assert "HEAD aaaaaaaaaaaa" in text
        assert "branch sbxloop/r1" in text
        assert "git fetch /state/runs/r1/workspace sbxloop/r1" in text

    def test_unmounted_run_warns_uncommitted_harvest(self, tmp_path: Path) -> None:
        text = self._summary(tmp_path, mounted=False)
        assert "harvested changes are uncommitted" in text
        assert "git fetch" not in text

    def test_run_without_clone_prints_nothing(self, tmp_path: Path) -> None:
        import sbxloop.cli.app as app_mod
        from sbxloop.config import Config
        from sbxloop.engine.model import RunResult

        config = Config.model_validate({"state_dir": str(tmp_path / "state")})
        StateStore(config.state_dir / "state.db").create_run("r1", "x")
        with app_mod.console.capture() as capture:
            app_mod._print_workspace_clone_summary(
                RunResult(run_id="r1", state="completed"), config
            )
        assert capture.get() == ""


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
        # in flight: no reason suffix
        assert "(" not in text.split("\n")[0]

    def test_reconciled_run_header_shows_reason(self) -> None:
        """#374: a reconciled run's reason appears in the TUI header."""
        from rich.console import Console

        from sbxloop.cli.tui import Dashboard

        dashboard = Dashboard()
        dashboard.on_event(Event.now("run.state", "r1", state="running"))
        dashboard.on_event(
            Event.now("run.reconciled", "r1", state="cancelled", reason="work item cancelled")
        )
        console = Console(record=True, width=100)
        console.print(dashboard.renderable())
        text = console.export_text()
        assert "cancelled" in text
        assert "work item cancelled" in text

    def test_roster_announcement_shows_all_tasks_waiting(self) -> None:
        """The engine announces every decomposed task up front; pending rows
        render as "waiting" until their turn instead of appearing one at a
        time as prior tasks complete."""
        from rich.console import Console

        from sbxloop.cli.tui import Dashboard

        dashboard = Dashboard()
        for event in [
            Event.now("run.state", "r1", state="running"),
            Event.now("task.state", "r1", task_id="t1", title="First task", state="pending"),
            Event.now("task.state", "r1", task_id="t2", title="Second task", state="pending"),
            Event.now("task.state", "r1", task_id="t3", title="Third task", state="pending"),
        ]:
            dashboard.on_event(event)

        console = Console(record=True, width=100)
        console.print(dashboard.renderable())
        text = console.export_text()
        for title in ("First task", "Second task", "Third task"):
            assert title in text
        assert text.count("waiting") == 3
        assert "pending" not in text

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

    def test_agent_message_header_names_the_persona(self) -> None:
        """Attributed messages title the chat bubble with the phase persona
        (planner, executor, ...); unattributed ones keep the generic
        "agent" title (covered above)."""
        from rich.console import Console

        from sbxloop.cli.tui import render_event

        rendered = render_event(
            Event.now("agent.message", "r1", content="looks good", agent="scrutinizer")
        )
        assert rendered is not None
        console = Console(record=True, width=80)
        console.print(rendered)
        assert "scrutinizer" in console.export_text()

    def test_agent_message_header_names_the_model(self) -> None:
        """When the event carries the answering model's slug, the chat
        bubble title shows it next to the persona; events without one keep
        the plain persona-and-timestamp header."""
        from rich.console import Console

        from sbxloop.cli.tui import render_event

        rendered = render_event(
            Event.now(
                "agent.message",
                "r1",
                content="done",
                agent="executor",
                model="claude-sonnet-5",
            )
        )
        assert rendered is not None
        console = Console(record=True, width=80)
        console.print(rendered)
        text = console.export_text()
        assert "executor" in text
        assert "claude-sonnet-5" in text

        rendered = render_event(Event.now("agent.message", "r1", content="done", agent="executor"))
        assert rendered is not None
        console = Console(record=True, width=80)
        console.print(rendered)
        assert "·" not in console.export_text()

    def test_format_event_includes_agent_name(self) -> None:
        from sbxloop.cli.tui import format_event

        line = format_event(Event.now("agent.message", "r1", agent="planner", content="hi"))
        assert "[planner]" in line
        assert "hi" in line

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

    def test_format_event_tool_command_is_readable(self) -> None:
        from sbxloop.cli.tui import format_event

        line = format_event(Event.now("agent.tool_start", "r1", tool="bash", args=RUN_CMD))
        assert "cd $RUN &&" in line
        assert "git diff" in line
        assert RUN_PATH not in line
        assert_no_silent_truncation(line)

    def test_format_event_tool_end_failure_includes_error(self) -> None:
        from sbxloop.cli.tui import format_event

        line = format_event(
            Event.now(
                "agent.tool_end",
                "r1",
                tool="bash",
                args="make lint",
                success=False,
                error="command not found: make",
            )
        )
        assert "bash" in line
        assert "make lint" in line
        assert "command not found: make" in line


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

    def wide_text(self, event: Event) -> str:
        """Render without terminal wrapping, so tokens split by line folding
        are not mistaken for truncation."""
        from rich.console import Console

        from sbxloop.cli.tui import render_event

        rendered = render_event(event)
        assert rendered is not None
        console = Console(record=True, width=300)
        console.print(rendered)
        return console.export_text()

    def test_tool_start_collapses_run_prefix_and_keeps_verb(self) -> None:
        text = self.wide_text(Event.now("agent.tool_start", "r1", tool="bash", args=RUN_CMD))
        assert text is not None
        assert "cd $RUN &&" in text
        assert "git diff" in text
        assert RUN_PATH not in text
        assert_no_silent_truncation(text)

    def test_tool_end_failure_collapses_run_prefix_and_keeps_verb(self) -> None:
        text = self.wide_text(
            Event.now(
                "agent.tool_end",
                "r1",
                tool="bash",
                args=RUN_CMD,
                success=False,
                exit_code=2,
                error="fatal: bad revision",
            )
        )
        assert text is not None
        assert "cd $RUN &&" in text
        assert "git diff" in text
        assert RUN_PATH not in text
        assert_no_silent_truncation(text)

    def test_rendering_leaves_stored_args_untouched(self) -> None:
        from sbxloop.cli.tui import format_event, render_event
        from sbxloop.events import summarize_event

        event = Event.now("agent.tool_start", "r1", tool="bash", args=RUN_CMD)
        summarize_event(event)
        format_event(event)
        render_event(event)
        assert event.data["args"] == RUN_CMD

    def test_summarize_event_still_clips_non_tool_args(self) -> None:
        from sbxloop.events import summarize_event

        event = Event.now("worker.exec", "r1", args="cd " + RUN_PATH + " && " + "y" * 300)
        summary = summarize_event(event)
        assert summary["args"].startswith("cd " + RUN_PATH[:20])
        assert len(summary["args"]) <= 120

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

    def test_tool_end_failure_echoes_what_ran(self) -> None:
        text = self.render_text(
            Event.now(
                "agent.tool_end",
                "r1",
                tool="bash",
                args="pytest -q tests/",
                success=False,
                exit_code=1,
            )
        )
        assert text is not None
        assert "✗ bash exit 1" in text
        assert "pytest -q tests/" in text

    def test_tool_end_failure_without_output_shows_error(self) -> None:
        # Failed executions carry only `error` (the SDK omits output/exit
        # code on failure); the reason must still reach the transcript.
        text = self.render_text(
            Event.now(
                "agent.tool_end",
                "r1",
                tool="glob",
                args="**/*.nope",
                success=False,
                error="no files matched pattern",
            )
        )
        assert text is not None
        assert "✗ glob" in text
        assert "**/*.nope" in text
        assert "no files matched pattern" in text

    def test_tool_end_with_only_error_still_renders_failure(self) -> None:
        text = self.render_text(
            Event.now("agent.tool_end", "r1", tool="bash", error="rejected by policy")
        )
        assert text is not None
        assert "✗ bash" in text
        assert "rejected by policy" in text


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


class TestResourceGauge:
    def sample_event(self, **data: Any) -> Any:
        from sbxloop.events import Event

        base: dict[str, Any] = {
            "role": "agent",
            "level": "ok",
            "disk_used_pct": 42.0,
            "mem_used_pct": 31.0,
            "load1": 0.5,
        }
        base.update(data)
        return Event.now("sandbox.resources", "r1", **base)

    def test_gauge_renders_in_status_panel(self) -> None:
        from rich.console import Console

        from sbxloop.cli.tui import Dashboard

        dashboard = Dashboard()
        dashboard.on_event(self.sample_event())
        dashboard.on_event(self.sample_event(role="github", disk_used_pct=12.0))
        console = Console(record=True, width=100)
        console.print(dashboard.renderable())
        text = console.export_text()
        assert "agent: disk 42%" in text
        assert "mem 31%" in text
        assert "load 0.5" in text
        assert "github: disk 12%" in text

    def test_gauge_escalates_past_thresholds(self) -> None:
        from rich.console import Console

        from sbxloop.cli.tui import Dashboard

        dashboard = Dashboard()
        dashboard.on_event(self.sample_event(level="abort", disk_used_pct=97.0))
        console = Console(record=True, width=100)
        console.print(dashboard.renderable())
        assert "⚠ abort" in console.export_text()

    def test_samples_stay_out_of_transcript(self) -> None:
        from sbxloop.cli.tui import render_event

        assert render_event(self.sample_event()) is None

    def test_warning_event_prints_to_transcript(self) -> None:
        from rich.console import Console

        from sbxloop.cli.tui import render_event
        from sbxloop.events import Event

        rendered = render_event(
            Event.now(
                "sandbox.resources_warning",
                "r1",
                level="warn",
                message="sandbox resources under pressure: disk 90.0% used (disk_warn: 85.0%)",
            )
        )
        assert rendered is not None
        console = Console(record=True, width=120)
        console.print(rendered)
        assert "disk 90.0% used" in console.export_text()

    def test_format_event_shows_resource_summary(self) -> None:
        from sbxloop.cli.tui import format_event

        line = format_event(self.sample_event(level="warn"))
        assert "disk=42.0%" in line
        assert "mem=31.0%" in line
        assert "warn" in line


class TestDoctorStatsProbe:
    def test_doctor_reports_in_vm_sampling(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GH_TOKEN", "tok")
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "sandbox stats" in result.output
        # fake sbx has no stats command -> in-VM sampling (real 0.38 has one)
        assert "samples in-VM" in result.output


MULTI_REPO_TOML = """
[[github.repos]]
repo = "acme/alpha"
deliver_base = "main"

[[github.repos]]
repo = "acme/beta"
token_env = "BETA_TOKEN"
enabled = false
"""


class TestMultiRepoCli:
    def test_status_shows_the_repo_a_run_targeted(self, workdir: Path) -> None:
        store = seed_store(workdir)
        from sbxloop.config import Config

        config = Config.model_validate({"github": {"repo": "acme/alpha"}})
        store.create_run("rmulti001", "second outcome", config.model_dump_json())
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "repo" in result.output
        assert "acme/alpha" in result.output

    def test_status_detail_shows_the_repo(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        store = seed_store(workdir)
        from sbxloop.config import Config

        config = Config.model_validate({"github": {"repo": "acme/beta"}})
        store.create_run("rmulti002", "an outcome", config.model_dump_json())
        result = runner.invoke(app, ["status", "rmulti002"])
        assert result.exit_code == 0
        assert "repo: acme/beta" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)

    def test_status_single_repo_shape_unchanged(self, workdir: Path) -> None:
        seed_store(workdir)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "rseeded11" in result.output and "completed" in result.output

    def test_daemon_items_show_their_repository(self, workdir: Path) -> None:
        from sbxloop.daemon.store import DaemonStore

        state = workdir / "xdg-state" / "sbxloop" / workdir.name
        dstore = DaemonStore(state / "state.db")
        dstore.upsert_new(
            WorkItem(
                item_id="gh:acme/alpha:issue:7",
                source_key="acme/alpha#7",
                title="alpha work",
                repo="acme/alpha",
            ),
            1.0,
        )
        dstore.close()
        result = runner.invoke(app, ["daemon", "items"])
        assert result.exit_code == 0, result.output
        assert "acme/alpha" in result.output

    def test_config_repos_lists_registered_repositories(self, workdir: Path) -> None:
        (workdir / "sbxloop.toml").write_text(MULTI_REPO_TOML)
        result = runner.invoke(app, ["config", "repos"])
        assert result.exit_code == 0, result.output
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "acme/alpha" in plain and "acme/beta" in plain
        assert "BETA_TOKEN" in plain
        assert "no" in plain  # beta is disabled

    def test_repo_selector_defaults_to_the_sole_repo(self, workdir: Path) -> None:
        (workdir / "sbxloop.toml").write_text('[github]\nrepo = "acme/only"\n')
        from sbxloop.cli.app import _resolve_repo
        from sbxloop.config import load_config

        assert _resolve_repo(load_config(), None).repo == "acme/only"

    def test_repo_selector_is_ambiguous_with_several_repos(self, workdir: Path) -> None:
        (workdir / "sbxloop.toml").write_text(
            '[[github.repos]]\nrepo = "acme/alpha"\n[[github.repos]]\nrepo = "acme/beta"\n'
        )
        import typer

        from sbxloop.cli.app import _resolve_repo
        from sbxloop.config import load_config

        config = load_config()
        with pytest.raises(typer.Exit):
            _resolve_repo(config, None)
        assert _resolve_repo(config, "beta").repo == "acme/beta"
        with pytest.raises(typer.Exit):
            _resolve_repo(config, "acme/nope")

    def test_config_repos_rejects_an_unknown_selector(self, workdir: Path) -> None:
        (workdir / "sbxloop.toml").write_text(MULTI_REPO_TOML)
        result = runner.invoke(app, ["config", "repos", "--repo", "acme/nope"])
        assert result.exit_code == 2
        assert "unknown repository" in result.output


class TestDoctorRepoChecks:
    def _config(self, toml: str, workdir: Path) -> Any:
        from sbxloop.config import load_config

        (workdir / "sbxloop.toml").write_text(toml)
        return load_config()

    def test_one_row_per_configured_repository(self, workdir: Path) -> None:
        from sbxloop.cli.doctor import repo_checks

        config = self._config(MULTI_REPO_TOML, workdir)
        rows = repo_checks(config, {"GH_TOKEN": "tok"})
        names = [row.name for row in rows]
        assert names == ["github repo acme/alpha", "github repo acme/beta"]
        assert all(row.ok for row in rows)
        assert "disabled" in rows[1].detail

    def test_missing_per_repo_token_fails_only_that_repo(self, workdir: Path) -> None:
        from sbxloop.cli.doctor import repo_checks

        config = self._config(
            '[[github.repos]]\nrepo = "acme/alpha"\n'
            '[[github.repos]]\nrepo = "acme/beta"\ntoken_env = "BETA_TOKEN"\n',
            workdir,
        )
        alpha, beta = repo_checks(config, {"GH_TOKEN": "tok"})
        assert alpha.ok
        assert not beta.ok and "BETA_TOKEN" in beta.detail

    def test_probe_results_are_reported_per_repo(self, workdir: Path) -> None:
        from sbxloop.cli.doctor import RepoProbe, repo_checks

        config = self._config(
            '[[github.repos]]\nrepo = "acme/alpha"\n[[github.repos]]\nrepo = "acme/beta"\n',
            workdir,
        )

        def probe(entry: Any) -> RepoProbe:
            if entry.repo == "acme/alpha":
                return RepoProbe(reachable=True, detail="reachable")
            return RepoProbe(reachable=True, missing_permissions=("issues:write",))

        alpha, beta = repo_checks(config, {"GH_TOKEN": "tok"}, probe=probe)
        assert alpha.ok and "reachable" in alpha.detail
        assert not beta.ok and "issues:write" in beta.detail

    def test_a_raising_probe_does_not_mask_the_other_repos(self, workdir: Path) -> None:
        from sbxloop.cli.doctor import RepoProbe, repo_checks

        config = self._config(
            '[[github.repos]]\nrepo = "acme/alpha"\n[[github.repos]]\nrepo = "acme/beta"\n',
            workdir,
        )

        def probe(entry: Any) -> RepoProbe:
            if entry.repo == "acme/alpha":
                raise RuntimeError("boom")
            return RepoProbe(reachable=True)

        alpha, beta = repo_checks(config, {"GH_TOKEN": "tok"}, probe=probe)
        assert not alpha.ok and "boom" in alpha.detail
        assert beta.ok

    def test_missing_repo_is_ok_when_create_repo_is_on(self, workdir: Path) -> None:
        from sbxloop.cli.doctor import RepoProbe, repo_checks

        config = self._config('[[github.repos]]\nrepo = "acme/new"\ncreate_repo = true\n', workdir)
        (row,) = repo_checks(
            config,
            {"GH_TOKEN": "tok"},
            probe=lambda _e: RepoProbe(reachable=False, creatable=True),
        )
        assert row.ok and "create_repo" in row.detail

    def test_an_unavailable_probe_is_unverified_not_a_failure(self, workdir: Path) -> None:
        """No github sandbox is "we could not ask", not "the repo is bad"."""
        from sbxloop.cli.doctor import RepoProbeUnavailable, repo_checks

        config = self._config('[[github.repos]]\nrepo = "acme/alpha"\n', workdir)

        def probe(_entry: Any) -> Any:
            raise RepoProbeUnavailable("no github sandbox (boom)")

        (row,) = repo_checks(config, {"GH_TOKEN": "tok"}, probe=probe)
        assert row.ok and not row.hard and "unverified" in row.detail

    def test_probe_reads_permissions_from_the_repo_payload(self) -> None:
        from sbxloop.cli.doctor import _missing_permissions

        assert _missing_permissions({"permissions": {"push": True}}) == ()
        assert _missing_permissions({"permissions": {"admin": True}}) == ()
        assert _missing_permissions({}) == ()  # not reported: no invented failure
        # Installation tokens answer all-False — including pull, which the
        # successful GET itself disproves — while holding full write on the
        # installation (field-verified 2026-08-31): not authoritative.
        all_false = dict.fromkeys(("admin", "maintain", "push", "triage", "pull"), False)
        assert _missing_permissions({"permissions": all_false}) == ()
        # ...while a genuinely read-only token (pull works, push denied)
        # still flags.
        assert _missing_permissions({"permissions": {"pull": True, "push": False}}) != ()
        missing = _missing_permissions({"permissions": {"pull": True, "push": False}})
        assert "issues:write" in missing and "contents:write" in missing

    def test_sandbox_probe_calls_repo_lookup_in_a_scoped_box(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One github-ops box per credential, scoped to the first repository
        on it (every repository on that credential shares the box, #515)."""
        import sbxloop.daemon.github as daemon_github
        from sbxloop.cli.doctor import sandbox_repo_probe

        seen: dict[str, object] = {}

        class FakeOps:
            def repo_lookup(self, repo: str) -> dict[str, object]:
                seen["looked_up"] = repo
                return {"permissions": {"push": True}}

        class FakeBox:
            def __init__(self, *_a: Any, repo: str | None = None, **_kw: Any) -> None:
                seen["scoped_to"] = repo
                self.closed = False

            def ops(self) -> FakeOps:
                return FakeOps()

            def close(self) -> None:
                seen["closed"] = True

        monkeypatch.setattr(daemon_github, "DaemonGithub", FakeBox)
        config = self._config('[[github.repos]]\nrepo = "acme/alpha"\n', workdir)
        boxes: dict[str, Any] = {}
        from sbxloop.sbx.cli import SbxCLI

        probe = sandbox_repo_probe(config, SbxCLI(), boxes=boxes)
        result = probe(config.github.repo_list()[0])
        assert result.reachable and not result.missing_permissions
        assert seen["scoped_to"] == "acme/alpha"
        assert seen["looked_up"] == "acme/alpha"
        assert set(boxes) == {""}, "keyed by credential: the daemon-wide token"

    def test_missing_repo_fails_without_create_repo(self, workdir: Path) -> None:
        from sbxloop.cli.doctor import RepoProbe, repo_checks

        config = self._config('[[github.repos]]\nrepo = "acme/gone"\n', workdir)
        (row,) = repo_checks(
            config, {"GH_TOKEN": "tok"}, probe=lambda _e: RepoProbe(reachable=False)
        )
        assert not row.ok

    def test_doctor_output_lists_every_repository(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GH_TOKEN", "tok")
        (workdir / "sbxloop.toml").write_text(MULTI_REPO_TOML)
        monkeypatch.setenv("COLUMNS", "300")
        result = runner.invoke(app, ["doctor"])
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "github repo acme/alpha" in plain
        assert "github repo acme/beta" in plain


class TestDoctorProbeCost:
    """#515: the probe boots one github sandbox per distinct credential, and
    only when asked — the default doctor provisions nothing."""

    TOML = (
        '[[github.repos]]\nrepo = "acme/alpha"\n\n'
        '[[github.repos]]\nrepo = "acme/beta"\n\n'
        '[[github.repos]]\nrepo = "acme/gamma"\ntoken_env = "GAMMA_TOKEN"\n'
    )

    class Box:
        """A DaemonGithub stand-in: records how it was built, serves lookups."""

        instances: ClassVar[list[Any]] = []
        fail_names: ClassVar[set[str]] = set()

        def __init__(self, config: Any, cli: Any, bus: Any, **kw: Any) -> None:
            self.name = kw.get("name")
            self.repo = kw.get("repo")
            self.closed = False
            self.lookups: list[str] = []
            self.ops_calls = 0
            type(self).instances.append(self)

        def ops(self) -> Any:
            self.ops_calls += 1
            if self.name in type(self).fail_names:
                raise RuntimeError("microVM would not boot")
            box = self

            class Ops:
                def repo_lookup(self, repo: str) -> dict[str, Any]:
                    box.lookups.append(repo)
                    return {"permissions": {"push": True, "pull": True}}

            return Ops()

        def close(self) -> None:
            self.closed = True

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.Box.instances = []
        self.Box.fail_names = set()
        import sbxloop.daemon.github as github_module

        monkeypatch.setattr(github_module, "DaemonGithub", self.Box)

    def _config(self, workdir: Path) -> Any:
        from sbxloop.config import load_config

        (workdir / "sbxloop.toml").write_text(self.TOML)
        return load_config()

    def test_one_sandbox_per_credential_not_per_repo(self, workdir: Path) -> None:
        from sbxloop.cli.doctor import RepoProbe, sandbox_repo_probe

        config = self._config(workdir)
        boxes: dict[str, Any] = {}
        probe = sandbox_repo_probe(config, cli=None, boxes=boxes)  # type: ignore[arg-type]
        results = [probe(entry) for entry in config.github.repo_list()]
        assert all(isinstance(r, RepoProbe) and r.reachable for r in results)
        assert sorted(boxes) == ["", "GAMMA_TOKEN"], "keyed by credential"
        assert len(self.Box.instances) == 2
        default, gamma = boxes[""], boxes["GAMMA_TOKEN"]
        assert default.name == "sbxloop-doctor-default" and default.repo == "acme/alpha"
        assert default.lookups == ["acme/alpha", "acme/beta"], "beta shares alpha's box"
        assert gamma.name == "sbxloop-doctor-gamma_token" and gamma.lookups == ["acme/gamma"]

    def test_a_credential_that_will_not_boot_answers_unverified_once(self, workdir: Path) -> None:
        from sbxloop.cli.doctor import RepoProbeUnavailable, repo_checks, sandbox_repo_probe

        config = self._config(workdir)
        self.Box.fail_names = {"sbxloop-doctor-default"}
        boxes: dict[str, Any] = {}
        probe = sandbox_repo_probe(config, cli=None, boxes=boxes)  # type: ignore[arg-type]
        entries = config.github.repo_list()
        with pytest.raises(RepoProbeUnavailable, match="microVM would not boot"):
            probe(entries[0])
        with pytest.raises(RepoProbeUnavailable):
            probe(entries[1])
        assert boxes[""].ops_calls == 1, "not re-provisioned for the second repo"
        assert probe(entries[2]).reachable, "the other credential is unaffected"
        rows = repo_checks(config, {"GH_TOKEN": "tok", "GAMMA_TOKEN": "g"}, probe=probe)
        assert [(r.ok, r.hard) for r in rows] == [(True, False), (True, False), (True, True)]
        assert "unverified" in rows[0].detail and "reachable" in rows[2].detail
        # run_doctor's teardown closes every box it opened, boot or no boot.
        for box in boxes.values():
            box.close()
        assert all(b.closed for b in self.Box.instances)

    def test_default_doctor_provisions_nothing(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._config(workdir)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GH_TOKEN", "tok")
        monkeypatch.setenv("GAMMA_TOKEN", "g")
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert self.Box.instances == [], "no github sandbox without --probe"
        assert "unverified" in result.output and "--probe" in result.output

    def test_probe_and_deep_boot_one_box_per_credential(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._config(workdir)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GH_TOKEN", "tok")
        monkeypatch.setenv("GAMMA_TOKEN", "g")
        result = runner.invoke(app, ["doctor", "--probe"])
        assert result.exit_code == 0, result.output
        assert sorted(b.name for b in self.Box.instances) == [
            "sbxloop-doctor-default",
            "sbxloop-doctor-gamma_token",
        ]
        assert all(b.closed for b in self.Box.instances), "torn down before the table"
        # The table folds the detail across lines with box borders between.
        flat = re.sub(r"[\s│]+", " ", result.output)
        assert "reachable, token has the required permissions" in flat
        self.Box.instances = []
        result = runner.invoke(app, ["doctor", "--deep"])
        assert len(self.Box.instances) == 2, "--deep implies --probe"


class TestDoctorRepoHealthRow:
    """#516: doctor shows the polling health the daemon persisted."""

    def test_suspended_and_backing_off_repos_are_flagged(self, workdir: Path) -> None:
        from sbxloop.cli.doctor import repo_checks
        from sbxloop.config import load_config

        (workdir / "sbxloop.toml").write_text(
            '[[github.repos]]\nrepo = "acme/alpha"\n\n[[github.repos]]\nrepo = "acme/beta"\n'
        )
        config = load_config()
        health = {
            "acme/alpha": {"suspended": True, "reason": "gone for this token (HTTP 404)"},
            "acme/beta": {"suspended": False, "next_poll": 99.0, "failures": 2},
        }
        alpha, beta = repo_checks(config, {"GH_TOKEN": "tok"}, health=health)
        assert alpha.ok and not alpha.hard
        assert (
            "SUSPENDED from polling by the daemon: gone for this token (HTTP 404)" in alpha.detail
        )
        assert "ctl resume-repo acme/alpha" in alpha.detail
        assert "backing off after 2 poll failure(s)" in beta.detail

    def test_health_is_read_from_the_daemon_store_only_when_it_exists(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        from sbxloop.cli.doctor import daemon_repo_health
        from sbxloop.config import load_config_with_sources
        from sbxloop.daemon.paths import resolve_state_dir
        from sbxloop.daemon.sources import REPO_HEALTH_KEY
        from sbxloop.daemon.store import DaemonStore

        monkeypatch.setenv("XDG_STATE_HOME", str(workdir / "xdg"))
        (workdir / "sbxloop.toml").write_text('[[github.repos]]\nrepo = "acme/alpha"\n')
        config, sources = load_config_with_sources()
        env = dict(os.environ)
        assert daemon_repo_health(config, sources, env) == {}
        state_dir = resolve_state_dir(config, sources, cwd=workdir, env=env, home=Path.home()).path
        state_dir.mkdir(parents=True, exist_ok=True)
        store = DaemonStore(state_dir / "state.db")
        store.set_value(
            REPO_HEALTH_KEY + "acme/alpha", json.dumps({"suspended": True, "reason": "x"})
        )
        store.close()
        got = daemon_repo_health(config, sources, env)
        assert got == {"acme/alpha": {"suspended": True, "reason": "x"}}


class TestDoctorBranchProtection:
    """Approving-review branch protection 405s every loop merge: doctor
    surfaces it as advice (human-out-of-the-loop doctrine)."""

    def _config(self, toml: str, workdir: Path) -> Any:
        from sbxloop.config import load_config

        (workdir / "sbxloop.toml").write_text(toml)
        return load_config()

    def test_protection_adds_a_soft_advisory_row(self, workdir: Path) -> None:
        from sbxloop.cli.doctor import RepoProbe, repo_checks

        config = self._config('[[github.repos]]\nrepo = "acme/alpha"\n', workdir)
        rows = repo_checks(
            config,
            {"GH_TOKEN": "tok"},
            probe=lambda _e: RepoProbe(reachable=True, review_protected=True),
        )
        main, protection = rows
        assert main.ok
        assert protection.name == "github repo acme/alpha branch protection"
        assert not protection.ok and not protection.hard
        assert "HTTP 405" in protection.detail
        assert "human-out-of-the-loop" in protection.detail

    def test_unverifiable_protection_adds_no_row(self, workdir: Path) -> None:
        from sbxloop.cli.doctor import RepoProbe, repo_checks

        config = self._config('[[github.repos]]\nrepo = "acme/alpha"\n', workdir)
        (row,) = repo_checks(
            config,
            {"GH_TOKEN": "tok"},
            probe=lambda _e: RepoProbe(reachable=True, review_protected=None),
        )
        assert row.ok

    def test_requires_approving_reviews_reads_both_sources(self) -> None:
        from sbxloop.cli.doctor import _requires_approving_reviews
        from sbxloop.errors import GithubOpsError

        class Ops:
            def __init__(self, protection: Any, rules: Any) -> None:
                self.protection, self.rules = protection, rules

            def raw(self, method: str, path: str) -> Any:
                answer = self.protection if path.endswith("/protection") else self.rules
                if isinstance(answer, Exception):
                    raise answer
                return answer

        classic = Ops({"required_pull_request_reviews": {"required_approving_review_count": 1}}, [])
        assert _requires_approving_reviews(classic, "o/r", "main") is True
        unprotected = Ops(GithubOpsError("x", http_status=404), [])
        assert _requires_approving_reviews(unprotected, "o/r", "main") is False
        ruleset = Ops(
            GithubOpsError("x", http_status=403),
            [{"type": "pull_request", "parameters": {"required_approving_review_count": 2}}],
        )
        assert _requires_approving_reviews(ruleset, "o/r", "main") is True
        unknown = Ops(GithubOpsError("x", http_status=403), GithubOpsError("x", http_status=403))
        assert _requires_approving_reviews(unknown, "o/r", "main") is None
