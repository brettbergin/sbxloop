"""Orphaned-sandbox classification and the `sandbox prune` / doctor surface."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sbxloop.cli.app import app
from sbxloop.engine.store import StateStore
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxInfo, SandboxSpec
from sbxloop.sbx.prune import classify_sandboxes, format_age
from tests.conftest import FakeSbx

runner = CliRunner()

HOUR = 3600.0


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state" / "state.db")


def info(name: str) -> SandboxInfo:
    return SandboxInfo(name=name, status="running")


class TestClassification:
    def test_non_sbxloop_sandboxes_ignored(self, store: StateStore) -> None:
        verdicts = classify_sandboxes([info("my-own-box"), info("sbx-other")], store)
        assert verdicts == []

    def test_unrecognized_sbxloop_name_never_orphaned(self, store: StateStore) -> None:
        # Future taxonomies (warm-pool standby etc.) must be safe from day
        # one: prefix-matching but unparseable names are reported, not pruned.
        verdicts = classify_sandboxes(
            [info("sbxloop-pool-agent"), info("sbxloop-oddball")], store, min_age_s=0.0
        )
        assert [v.orphan for v in verdicts] == [False, False]
        assert all("unrecognized" in v.reason for v in verdicts)

    def test_unknown_run_is_orphan_with_multi_host_honesty(self, store: StateStore) -> None:
        (verdict,) = classify_sandboxes([info("sbxloop-rabc12345-agent")], store)
        assert verdict.orphan
        assert verdict.run_id == "rabc12345"
        assert verdict.role == "agent"
        assert verdict.run_state is None
        assert verdict.age_s is None
        assert "this state DB" in verdict.reason

    def test_terminal_run_orphaned_only_past_min_age(self, store: StateStore) -> None:
        store.create_run("rabc12345", "x")
        store.set_run_state("rabc12345", "completed")
        now = store.get_run("rabc12345").updated_at

        (recent,) = classify_sandboxes(
            [info("sbxloop-rabc12345-agent")], store, min_age_s=HOUR, now=now + 60
        )
        assert not recent.orphan
        assert "younger than --min-age" in recent.reason

        (old,) = classify_sandboxes(
            [info("sbxloop-rabc12345-agent")], store, min_age_s=HOUR, now=now + 2 * HOUR
        )
        assert old.orphan
        assert old.run_state == "completed"

    @pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
    def test_all_terminal_states_prunable(self, store: StateStore, state: str) -> None:
        store.create_run("rabc12345", "x")
        store.set_run_state("rabc12345", state)  # type: ignore[arg-type]
        now = store.get_run("rabc12345").updated_at
        (verdict,) = classify_sandboxes(
            [info("sbxloop-rabc12345-github")], store, min_age_s=HOUR, now=now + 2 * HOUR
        )
        assert verdict.orphan

    def test_stale_non_terminal_run_is_orphan(self, store: StateStore) -> None:
        store.create_run("rabc12345", "x")
        store.set_run_state("rabc12345", "running")
        now = store.get_run("rabc12345").updated_at
        (verdict,) = classify_sandboxes(
            [info("sbxloop-rabc12345-agent")], store, min_age_s=HOUR, now=now + 2 * HOUR
        )
        assert verdict.orphan
        assert "silent" in verdict.reason

    def test_recent_events_keep_a_non_terminal_run_alive(self, store: StateStore) -> None:
        """A long phase does not bump runs.updated_at — liveness must come
        from the persisted event stream (heartbeats included)."""
        from sbxloop_worker.protocol import Event

        store.create_run("rabc12345", "x")
        store.set_run_state("rabc12345", "running")
        now = store.get_run("rabc12345").updated_at + 2 * HOUR
        store.append_event(Event(ts=now - 60, run_id="rabc12345", type="worker.heartbeat", data={}))
        (verdict,) = classify_sandboxes(
            [info("sbxloop-rabc12345-agent")], store, min_age_s=HOUR, now=now
        )
        assert not verdict.orphan
        assert "possibly live" in verdict.reason

    def test_kept_runs_excluded_unless_included(self, store: StateStore) -> None:
        store.create_run("rabc12345", "x")
        store.set_run_state("rabc12345", "failed")
        store.set_run_kept("rabc12345", "debug")
        now = store.get_run("rabc12345").updated_at + 2 * HOUR

        (kept,) = classify_sandboxes(
            [info("sbxloop-rabc12345-agent")], store, min_age_s=HOUR, now=now
        )
        assert not kept.orphan
        assert "kept (debug)" in kept.reason

        (included,) = classify_sandboxes(
            [info("sbxloop-rabc12345-agent")],
            store,
            min_age_s=HOUR,
            now=now,
            include_kept=True,
        )
        assert included.orphan
        assert included.kept_reason == "debug"

    def test_format_age(self) -> None:
        assert format_age(None) == "?"
        assert format_age(120) == "2m"
        assert format_age(2 * HOUR) == "2.0h"
        assert format_age(3 * 86400) == "3.0d"


class TestPruneCommand:
    def seed(self, workdir: Path, fake_sbx: FakeSbx, run_id: str, state: str) -> StateStore:
        store = StateStore(workdir / ".sbxloop" / "state.db")
        store.create_run(run_id, "an outcome")
        store.set_run_state(run_id, state)  # type: ignore[arg-type]
        cli = SbxCLI(binary=str(fake_sbx.binary))
        for role in ("agent", "github"):
            cli.create(
                SandboxSpec(name=f"sbxloop-{run_id}-{role}", role="agent", workspace=workdir)
            )
        return store

    def test_dry_run_by_default(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        self.seed(workdir, fake_sbx, "rabc12345", "failed")
        result = runner.invoke(app, ["sandbox", "prune", "--min-age", "0"])
        assert result.exit_code == 0, result.output
        assert "dry run" in result.output
        assert "orphan" in result.output
        # nothing was removed
        assert len(SbxCLI(binary=str(fake_sbx.binary)).ls()) == 2

    def test_force_removes_orphans(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        self.seed(workdir, fake_sbx, "rabc12345", "failed")
        result = runner.invoke(app, ["sandbox", "prune", "--min-age", "0", "--force"])
        assert result.exit_code == 0, result.output
        assert result.output.count("removed") == 2
        assert SbxCLI(binary=str(fake_sbx.binary)).ls() == []
        # stop-then-rm, mirroring pair cleanup
        assert fake_sbx.invocations("stop")
        assert fake_sbx.invocations("rm")

    def test_force_removes_run_sandbox_secrets_too(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        """``sbx rm`` leaves secret registrations behind; a later provision
        under the same name (daemon resume) cannot replace them and the
        agent boots with the proxy sentinel (field failure rgn9ccjam).
        Prune must take the registrations with the sandbox — verified by
        re-registering afterwards, which real sbx (and the fake) refuse
        while a registration lingers."""
        self.seed(workdir, fake_sbx, "rabc12345", "failed")
        cli = SbxCLI(binary=str(fake_sbx.binary))
        cli.secret_set_custom(
            host="api.github.com",
            env="COPILOT_GITHUB_TOKEN",
            value="ghp_x",
            sandbox="sbxloop-rabc12345-agent",
        )
        cli.secret_set("github", sandbox="sbxloop-rabc12345-github", token="ghp_y")
        result = runner.invoke(app, ["sandbox", "prune", "--min-age", "0", "--force"])
        assert result.exit_code == 0, result.output
        assert cli.ls() == []
        # Both registrations are gone: re-registering succeeds.
        cli.secret_set_custom(
            host="api.github.com",
            env="COPILOT_GITHUB_TOKEN",
            value="ghp_x",
            sandbox="sbxloop-rabc12345-agent",
        )
        cli.secret_set("github", sandbox="sbxloop-rabc12345-github", token="ghp_y")

    def test_recent_run_not_pruned(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        self.seed(workdir, fake_sbx, "rabc12345", "failed")
        result = runner.invoke(app, ["sandbox", "prune", "--force"])  # default 1h min-age
        assert result.exit_code == 0, result.output
        assert "nothing to prune" in result.output
        assert len(SbxCLI(binary=str(fake_sbx.binary)).ls()) == 2

    def test_kept_run_survives_force_without_include_kept(
        self, workdir: Path, fake_sbx: FakeSbx
    ) -> None:
        store = self.seed(workdir, fake_sbx, "rabc12345", "failed")
        store.set_run_kept("rabc12345", "debug")
        result = runner.invoke(app, ["sandbox", "prune", "--min-age", "0", "--force"])
        assert result.exit_code == 0, result.output
        assert len(SbxCLI(binary=str(fake_sbx.binary)).ls()) == 2

        included = runner.invoke(
            app, ["sandbox", "prune", "--min-age", "0", "--force", "--include-kept"]
        )
        assert included.exit_code == 0, included.output
        assert SbxCLI(binary=str(fake_sbx.binary)).ls() == []
        # pruning a kept run clears its marker
        assert store.get_run("rabc12345").kept_reason is None

    def test_unknown_sandbox_pruned_with_caveat(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        StateStore(workdir / ".sbxloop" / "state.db")  # empty DB
        cli = SbxCLI(binary=str(fake_sbx.binary))
        cli.create(SandboxSpec(name="sbxloop-rzzzzzzzz-agent", role="agent", workspace=workdir))
        result = runner.invoke(app, ["sandbox", "prune"])
        assert result.exit_code == 0, result.output
        assert "another checkout" in result.output or "working copy" in result.output
        forced = runner.invoke(app, ["sandbox", "prune", "--force"])
        assert forced.exit_code == 0, forced.output
        assert cli.ls() == []

    def test_no_sandboxes(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        result = runner.invoke(app, ["sandbox", "prune"])
        assert result.exit_code == 0, result.output
        assert "no sbxloop sandboxes" in result.output

    def test_rm_failure_reported_and_exit_1(self, workdir: Path, fake_sbx: FakeSbx) -> None:
        self.seed(workdir, fake_sbx, "rabc12345", "failed")
        fake_sbx.script("rm", returncode=1, stderr="daemon busy")
        result = runner.invoke(app, ["sandbox", "prune", "--min-age", "0", "--force"])
        assert result.exit_code == 1
        assert "skip" in result.output


class TestDoctorOrphans:
    def test_doctor_warns_on_orphans(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        StateStore(workdir / ".sbxloop" / "state.db")  # empty DB → unknown sandbox
        SbxCLI(binary=str(fake_sbx.binary)).create(
            SandboxSpec(name="sbxloop-rzzzzzzzz-agent", role="agent", workspace=workdir)
        )
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output  # soft warning, not a failure
        assert "orphan candidate" in result.output
        assert "sandbox prune" in result.output

    def test_doctor_clean_when_no_orphans(
        self, workdir: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok")
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "orphaned sandboxes" in result.output
        assert "none found" in result.output


class TestFreshRunAge:
    def test_just_created_run_is_young(self, store: StateStore) -> None:
        # A run created moments ago must read as young with real wall time.
        store.create_run("rabc12345", "x")
        (verdict,) = classify_sandboxes([info("sbxloop-rabc12345-agent")], store)
        assert not verdict.orphan
        assert verdict.age_s is not None
        assert verdict.age_s < 60
        assert time.time() >= store.get_run("rabc12345").created_at
