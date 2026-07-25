"""CLI tests via typer's CliRunner, fake sbx, and the scripted echo backend."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, ClassVar

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
        # A run whose driving process died hard stays `running` in the DB
        # forever; --follow must notice the silence and exit, not spin.
        store = seed_store(workdir)
        store.set_run_state("rseeded11", "running")
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
        assert "execute" in result.output
        assert "plan-declared grants" in result.output
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

    def _bake_record(self, workdir: Path, *, worker_version: str, ref: str) -> None:
        state = workdir / ".sbxloop"
        state.mkdir(exist_ok=True)
        (state / "bake.json").write_text(
            json.dumps(
                {
                    "ref": ref,
                    "worker_version": worker_version,
                    "python": "/home/agent/.sbxloop/venv/bin/python",
                    "runtime_cached": True,
                    "baked_at": 0.0,
                }
            )
        )

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
        cached = load_verdicts(workdir / ".sbxloop", "0.35.0")
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
            PROBE_SECRET_ENV_VISIBILITY,
            ProbeRecord,
            save_verdicts,
        )

        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        save_verdicts(
            workdir / ".sbxloop",
            "0.35.0",
            {
                PROBE_SECRET_ENV_VISIBILITY: ProbeRecord(
                    verdict="visible-under-exec", checked_at=time_module.time()
                )
            },
        )
        result = runner.invoke(app, ["doctor"])
        assert "sbx drift" in result.output
        # drift warns loudly but does not fail an otherwise-ready host
        assert result.exit_code == 0, result.output


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
        {"json": {"steps": ["do"], "expected_artifacts": [], "verify_commands": []}},
        {"text": "did it"},
        {"json": {"verdict": "pass"}},
        {"json": {"verdict": "accept"}},
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
                {"json": {"steps": ["do"], "expected_artifacts": [], "verify_commands": []}},
                {"text": "did it"},
                {"json": {"verdict": "pass"}},
                {"json": {"verdict": "accept"}},
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
        monkeypatch.setenv("SBXLOOP_GITHUB__REPO", "o/r")
        result = runner.invoke(app, ["run", "ship it", "--no-tui", "--deliver"])
        assert result.exit_code == 0, result.output
        assert delivered == ["o/r"]
        assert "run.deliver" in result.output
        assert "pull/8" in result.output

    def test_run_deliver_refused_without_github_config(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.make_run_env(workdir, monkeypatch, [])
        result = runner.invoke(app, ["run", "ship it", "--no-tui", "--deliver"])
        assert result.exit_code == 2
        assert "GitHub integration is not configured" in result.output
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
        revise = {"json": {"verdict": "revise", "feedback": "nope"}}
        plan = {"json": {"steps": ["do"], "expected_artifacts": [], "verify_commands": []}}
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
                                "verify_commands": ["true"],
                            }
                        ]
                    }
                },
                plan,
                *[execute, revise] * 3,
            ],
        )
        monkeypatch.setenv("SBXLOOP_KEEP_ON_FAILURE", "true")
        result = runner.invoke(app, ["run", "doomed", "--no-tui"])
        assert result.exit_code == 1, result.output
        assert "sandboxes kept:" in result.output
        assert "sbxloop shell" in result.output
        assert "sandbox rm --run" in result.output


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
        # fake sbx (like real 0.35.x) has no stats command -> in-VM sampling
        assert "samples in-VM" in result.output
