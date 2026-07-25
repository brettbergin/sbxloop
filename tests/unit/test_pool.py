"""Warm pool tests: fingerprint keying, warmup, claim hygiene, TTL, CLI."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import sbxloop
from sbxloop.cli.app import app
from sbxloop.config import Config
from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.store import StateStore
from sbxloop.events import Event, EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.pool import WarmPool, provision_fingerprint
from tests.conftest import FakeSbx

TOKENS = {"COPILOT_GITHUB_TOKEN": "github_pat_copilot", "GH_TOKEN": "github_pat_user"}

GITHUB_ENABLED = {"github": {"repo": "owner/repo"}}


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    return Config.model_validate(
        {
            "state_dir": str(tmp_path / "state"),
            # Unit tests exercise provisioning/reuse, not the install ladder.
            "install_workers": False,
            **GITHUB_ENABLED,
            **overrides,
        }
    )


def make_pool(
    fake_sbx: FakeSbx,
    config: Config,
    *,
    store: StateStore | None = None,
    clock: Clock | None = None,
    env: dict[str, str] | None = None,
    bus: EventBus | None = None,
) -> WarmPool:
    return WarmPool(
        SbxCLI(binary=str(fake_sbx.binary)),
        config,
        store or StateStore(config.state_dir / "state.db"),
        bus=bus,
        env=TOKENS if env is None else env,
        clock=clock or Clock(),
    )


class TestFingerprint:
    def test_stable_for_same_config(self, tmp_path: Path) -> None:
        assert provision_fingerprint(make_config(tmp_path)) == provision_fingerprint(
            make_config(tmp_path)
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"sandbox": {"template": "docker.io/x/tpl:v1"}},
            {"sandbox": {"extra_allow_domains": ["internal.example.com"]}},
            {"secret_strategy": "plain-env"},
            {"github": {}},  # github disabled → agent-only pair
            {"app_name": "isolated"},
            {"install_workers": True},
        ],
    )
    def test_changes_with_provision_inputs(self, tmp_path: Path, overrides: dict[str, Any]) -> None:
        base = provision_fingerprint(make_config(tmp_path))
        assert provision_fingerprint(make_config(tmp_path, **overrides)) != base

    def test_changes_with_version(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        base = provision_fingerprint(make_config(tmp_path))
        monkeypatch.setattr(sbxloop, "__version__", "0.0.0-somethingelse")
        assert provision_fingerprint(make_config(tmp_path)) != base


class TestWarmup:
    def test_provisions_pool_named_pair_and_records_it(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        config = make_config(tmp_path)
        pool = make_pool(fake_sbx, config)
        (record,) = pool.warmup()

        assert record.agent_name == f"sbxloop-pool-{record.pool_id}-agent"
        assert record.github_name == f"sbxloop-pool-{record.pool_id}-github"
        created = [c[1].removeprefix("--name=") for c in fake_sbx.invocations("create")]
        assert created == [record.agent_name, record.github_name]
        # pool-owned workspace, not a run workspace
        assert record.workspace == (config.state_dir / "pool" / record.pool_id / "workspace")
        assert [r.pool_id for r in pool.store.list_pool_pairs()] == [record.pool_id]

    def test_ttl_stamped_from_config(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        config = make_config(tmp_path, pool={"ttl_s": 60.0})
        clock = Clock(5000.0)
        pool = make_pool(fake_sbx, config, clock=clock)
        (record,) = pool.warmup()
        assert record.expires_at == pytest.approx(5060.0)

    def test_count_provisions_multiple(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        pool = make_pool(fake_sbx, make_config(tmp_path))
        records = pool.warmup(count=2)
        assert len({r.pool_id for r in records}) == 2
        assert len(pool.store.list_pool_pairs()) == 2


class TestClaim:
    def test_claim_returns_pair_and_consumes_record(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        config = make_config(tmp_path)
        events: list[Event] = []
        bus = EventBus()
        bus.subscribe(events.append)
        pool = make_pool(fake_sbx, config, bus=bus)
        (record,) = pool.warmup()

        pair = pool.claim("r1aaaaaaa")
        assert pair is not None
        assert pair.run_id == "r1aaaaaaa"
        assert pair.agent.name == record.agent_name
        assert pair.github is not None and pair.github.name == record.github_name
        assert pair.preinstalled
        assert pair.workspace == record.workspace
        assert pool.store.list_pool_pairs() == []
        assert "sandbox.pool_claim" in [e.type for e in events]
        # the standby is consumed: a second claim goes cold
        assert pool.claim("r2aaaaaaa") is None

    def test_claim_refreshes_secrets(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        pool = make_pool(fake_sbx, make_config(tmp_path))
        pool.warmup()
        sets_before = len([s for s in fake_sbx.secrets() if s["args"][0].startswith("set")])
        assert pool.claim("r1aaaaaaa") is not None
        sets_after = len([s for s in fake_sbx.secrets() if s["args"][0].startswith("set")])
        assert sets_after > sets_before

    def test_claim_resets_run_scoped_state(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        pool = make_pool(fake_sbx, config)
        (record,) = pool.warmup()
        # leftovers a previous run could have produced
        stale_artifact = record.workspace / "stale.txt"
        stale_artifact.write_text("leak")
        jobs_dir = fake_sbx.sandbox_fs(record.agent_name) / "home/agent/.sbxloop/jobs"
        stale_job = jobs_dir / "stale-job.json"
        stale_job.parent.mkdir(parents=True, exist_ok=True)
        stale_job.write_text("{}")

        assert pool.claim("r1aaaaaaa") is not None
        assert not stale_artifact.exists()
        assert not stale_job.exists()

    def test_claim_ignores_fingerprint_mismatch(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        pool = make_pool(fake_sbx, config)
        pool.warmup()
        changed = make_config(tmp_path, sandbox={"extra_allow_domains": ["internal.example.com"]})
        other = make_pool(fake_sbx, changed, store=pool.store)
        assert other.claim("r1aaaaaaa") is None
        # the mismatched standby is left alone for a matching future run
        assert len(pool.store.list_pool_pairs()) == 1

    def test_claim_discards_dead_pair(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        pool = make_pool(fake_sbx, config)
        (record,) = pool.warmup()
        # the sandbox vanished out-of-band (manual `sbx rm`, host reboot, ...)
        SbxCLI(binary=str(fake_sbx.binary)).rm(record.agent_name)

        assert pool.claim("r1aaaaaaa") is None
        assert pool.store.list_pool_pairs() == []
        # the surviving half of the pair was torn down too
        assert not (fake_sbx.state / "sandboxes" / record.github_name).exists()

    def test_expired_pair_is_pruned_not_claimed(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        config = make_config(tmp_path, pool={"ttl_s": 60.0})
        clock = Clock(1000.0)
        pool = make_pool(fake_sbx, config, clock=clock)
        (record,) = pool.warmup()

        clock.now = 1061.0
        assert pool.claim("r1aaaaaaa") is None
        assert pool.store.list_pool_pairs() == []
        for name in (record.agent_name, record.github_name):
            assert name is not None
            assert not (fake_sbx.state / "sandboxes" / name).exists()

    def test_missing_host_token_fails_loud(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        """Token absence must fail the run exactly like cold provisioning."""
        from sbxloop.errors import ProvisionError

        config = make_config(tmp_path)
        pool = make_pool(fake_sbx, config)
        pool.warmup()
        broke = make_pool(fake_sbx, config, store=pool.store, env={})
        with pytest.raises(ProvisionError, match="COPILOT_GITHUB_TOKEN"):
            broke.claim("r1aaaaaaa")
        # the standby was not burned by the failed claim
        assert len(pool.store.list_pool_pairs()) == 1


# -- engine integration --------------------------------------------------


def taskgraph_script() -> list[dict[str, Any]]:
    return [
        {
            "json": {
                "tasks": [
                    {
                        "id": "t1",
                        "title": "Task t1",
                        "description": "d",
                        "depends_on": [],
                        "acceptance_criteria": ["works"],
                        "verify_commands": ["true"],
                    }
                ]
            }
        },
        {"json": {"steps": ["do"], "expected_artifacts": [], "verify_commands": []}},
        {"text": "work complete"},
        {"json": {"verdict": "pass"}},
        {"json": {"verdict": "accept"}},
    ]


class TestEngineClaim:
    def test_run_claims_warm_pair(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script_path = tmp_path / "echo-script.json"
        script_path.write_text(json.dumps(taskgraph_script()))
        monkeypatch.setenv("SBXLOOP_WORKER_BACKEND", "echo")
        monkeypatch.setenv("SBXLOOP_ECHO_SCRIPT", str(script_path))
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot_tok")
        monkeypatch.setenv("GH_TOKEN", "gh_tok")

        config = make_config(tmp_path, github={}, worker_python=sys.executable)
        store = StateStore(config.state_dir / "state.db")
        # warm with a real-time clock: the engine claims with time.time()
        warm = make_pool(fake_sbx, config, store=store, clock=Clock(time.time()))
        (record,) = warm.warmup()
        creates_before = len(fake_sbx.invocations("create"))

        events: list[Event] = []
        bus = EventBus()
        bus.subscribe(events.append)
        engine = LoopEngine(config, store=store, bus=bus, sbx=SbxCLI(binary=str(fake_sbx.binary)))
        result = engine.start("reuse the warm pair")

        assert result.succeeded
        # the run rode the standby: no new sandboxes were created
        assert len(fake_sbx.invocations("create")) == creates_before
        assert "sandbox.pool_claim" in [e.type for e in events]
        assert result.workspace == record.workspace
        assert store.list_pool_pairs() == []
        # normal teardown applies to claimed pairs
        boxes = fake_sbx.state / "sandboxes"
        assert not boxes.is_dir() or list(boxes.iterdir()) == []

    def test_resume_never_claims(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resumed run keeps its persisted run workspace — the standby's
        freshly reset workspace would silently drop the partial work."""
        monkeypatch.setenv("SBXLOOP_WORKER_BACKEND", "echo")
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot_tok")

        config = make_config(tmp_path, github={}, worker_python=sys.executable)
        store = StateStore(config.state_dir / "state.db")
        warm = make_pool(fake_sbx, config, store=store, clock=Clock(time.time()))
        warm.warmup()

        # an interrupted run whose only task already finished: resume just
        # re-provisions and finalizes, consuming no agent phases
        from sbxloop.engine.model import TaskSpec

        store.create_run("rresume11", "finish up")
        store.set_run_state("rresume11", "running")
        store.save_tasks("rresume11", [TaskSpec(id="t1", title="Task t1")])
        (task_record,) = store.get_tasks("rresume11")
        task_record.state = "done"
        store.update_task("rresume11", task_record)

        engine = LoopEngine(config, store=store, sbx=SbxCLI(binary=str(fake_sbx.binary)))
        result = engine.resume("rresume11")
        assert result.succeeded
        # the standby was untouched; the resume provisioned a run-scoped pair
        assert len(store.list_pool_pairs()) == 1
        assert result.workspace == config.state_dir.resolve() / "runs/rresume11/workspace"

    def test_run_falls_back_cold_when_pool_empty(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script_path = tmp_path / "echo-script.json"
        script_path.write_text(json.dumps(taskgraph_script()))
        monkeypatch.setenv("SBXLOOP_WORKER_BACKEND", "echo")
        monkeypatch.setenv("SBXLOOP_ECHO_SCRIPT", str(script_path))
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot_tok")

        config = make_config(tmp_path, github={}, worker_python=sys.executable)
        engine = LoopEngine(config, sbx=SbxCLI(binary=str(fake_sbx.binary)))
        result = engine.start("cold start still works")
        assert result.succeeded
        created = [c[1].removeprefix("--name=") for c in fake_sbx.invocations("create")]
        assert created == [f"sbxloop-{result.run_id}-agent"]


# -- CLI -------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def cli_workdir(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workdir = tmp_path / "project"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    workdir.joinpath("sbxloop.toml").write_text("install_workers = false\n")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot_tok")
    return workdir


class TestCli:
    def test_warmup_then_pool_then_rm_all(self, cli_workdir: Path, fake_sbx: FakeSbx) -> None:
        result = runner.invoke(app, ["warmup"])
        assert result.exit_code == 0, result.output
        assert "warm pair" in result.output

        (record,) = StateStore(cli_workdir / ".sbxloop" / "state.db").list_pool_pairs()
        listing = runner.invoke(app, ["sandbox", "pool"])
        assert listing.exit_code == 0
        assert record.pool_id in listing.output
        assert "yes" in listing.output

        removed = runner.invoke(app, ["sandbox", "rm", "--all"])
        assert removed.exit_code == 0
        assert "dropped 1 warm-pool record" in removed.output
        assert not (fake_sbx.state / "sandboxes").is_dir() or not list(
            (fake_sbx.state / "sandboxes").iterdir()
        )
        relisted = runner.invoke(app, ["sandbox", "pool"])
        assert record.pool_id not in relisted.output

    def test_warmup_count(self, cli_workdir: Path, fake_sbx: FakeSbx) -> None:
        result = runner.invoke(app, ["warmup", "--count", "2"])
        assert result.exit_code == 0, result.output
        creates = fake_sbx.invocations("create")
        assert len(creates) == 2  # github disabled → agent-only pairs
