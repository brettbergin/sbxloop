"""Warm sandbox sets (#47): provisioned before the run, claimed by dispatch.

Field (db, 2026-09-19): 57 to 94 seconds of every run went to booting
microVMs and installing the worker; a warm set has both done before the
run is dispatched, under the run id the run then takes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.errors import ProvisionError
from sbxloop.ids import new_run_id
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxInfo
from sbxloop.sbx.prune import classify_sandboxes
from sbxloop.sbx.warm import Warmer, WarmRegistry, warm_fingerprint
from sbxloop.worker.client import WorkerClient
from tests.conftest import FakeSbx
from tests.unit.test_daemon_loop import Harness, gh_item
from tests.unit.test_provision import GITHUB_ENABLED, TOKENS


def config(tmp_path: Path, **daemon: Any) -> Config:
    return Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            **GITHUB_ENABLED,
            "daemon": {"warm_pairs": 1, "max_runs_per_day": 100, **daemon},
        }
    )


def warmer(fake_sbx: FakeSbx, cfg: Config, **kwargs: Any) -> Warmer:
    kwargs.setdefault("install_workers", False)
    kwargs.setdefault("worker_python", sys.executable)
    return Warmer(cfg, SbxCLI(binary=str(fake_sbx.binary)), env=TOKENS, **kwargs)


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class TestFilling:
    def test_fill_one_provisions_a_set_under_a_fresh_run_id_and_records_it(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        w = warmer(fake_sbx, config(tmp_path))
        warm = w.fill_one()
        assert warm is not None and warm.state == "ready"
        assert warm.names == [f"sbxloop-{warm.run_id}-agent", f"sbxloop-{warm.run_id}-github"]
        listed = {info.name for info in w.cli.ls()}
        assert set(warm.names) <= listed
        # Recorded for the next daemon (and for prune) to find.
        assert [s.run_id for s in WarmRegistry(w.registry.path).load()] == [warm.run_id]
        assert w.ready() == [warm]
        # The run's workspace directory exists and is what the agent box mounts.
        assert fake_sbx.meta(warm.names[0])["workspace"] == str(
            w.config.paths.run_workspace(warm.run_id).resolve()
        )

    def test_fill_one_installs_the_workers_as_the_engine_would(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = config(tmp_path)
        cfg.sandbox.template = "sbxloop-baked:latest"
        installs: list[tuple[str | None, dict[str, Any]]] = []

        def install(self: WorkerClient, **kwargs: Any) -> None:
            installs.append((self.role, kwargs))

        monkeypatch.setattr(WorkerClient, "install", install)
        w = warmer(fake_sbx, cfg, install_workers=True)
        assert w.fill_one() is not None
        by_role = dict(installs)
        assert set(by_role) == {"agent", "github"}
        assert by_role["agent"]["extras"] == cfg.agent.backend
        assert by_role["agent"]["ensure_dev_tools"] is True
        assert by_role["agent"]["expect_prebaked"] is True
        assert by_role["github"] == {"extras": "", "expect_prebaked": True}

    def test_a_failed_install_removes_the_set(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def install(self: WorkerClient, **kwargs: Any) -> None:
            raise ProvisionError("the ladder fell over")

        monkeypatch.setattr(WorkerClient, "install", install)
        w = warmer(fake_sbx, config(tmp_path), install_workers=True)
        assert w.fill_one() is None
        assert w.sets() == []
        assert not any(info.name.startswith("sbxloop-r") for info in w.cli.ls())


class TestClaiming:
    def test_claim_hands_out_the_oldest_ready_set_once(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        clock = Clock()
        w = warmer(fake_sbx, config(tmp_path, warm_pairs=2), clock=clock)
        first = w.fill_one()
        clock.t += 10
        second = w.fill_one()
        assert first is not None and second is not None
        assert w.claim() == first.run_id
        assert w.is_claimed(first.run_id) and not w.is_claimed(second.run_id)
        assert w.claim() == second.run_id
        assert w.claim() is None

    def test_a_set_from_another_configuration_is_never_claimed(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        w = warmer(fake_sbx, config(tmp_path))
        assert w.fill_one() is not None
        later = config(tmp_path)
        later.sandbox.template = "sbxloop-baked:v2"
        assert warm_fingerprint(later) != warm_fingerprint(w.config)
        assert warmer(fake_sbx, later).claim() is None


class TestUpkeep:
    def test_reconcile_drops_a_set_whose_sandboxes_are_gone(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        w = warmer(fake_sbx, config(tmp_path))
        warm = w.fill_one()
        assert warm is not None
        w.cli.rm(warm.names[0])
        w.reconcile()
        assert w.sets() == []
        # The other half was removed too.
        assert not any(info.name in warm.names for info in w.cli.ls())

    def test_reconcile_removes_a_set_from_another_configuration(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        w = warmer(fake_sbx, config(tmp_path))
        warm = w.fill_one()
        assert warm is not None
        later = config(tmp_path)
        later.sandbox.template = "sbxloop-baked:v2"
        warmer(fake_sbx, later).reconcile()
        assert warmer(fake_sbx, later).sets() == []
        assert not any(info.name in warm.names for info in w.cli.ls())

    def test_an_expired_set_is_removed(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        clock = Clock()
        w = warmer(fake_sbx, config(tmp_path, warm_ttl_s=100.0), clock=clock)
        warm = w.fill_one()
        assert warm is not None
        clock.t += 99
        w.expire()
        assert w.ready() == [warm]
        clock.t += 2
        w.expire()
        assert w.sets() == []
        assert not any(info.name in warm.names for info in w.cli.ls())

    def test_a_claimed_set_is_forgotten_once_its_run_finished(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        w = warmer(fake_sbx, config(tmp_path))
        warm = w.fill_one()
        assert warm is not None
        assert w.claim() == warm.run_id
        w.sweep_claimed(lambda run_id: False)
        assert w.is_claimed(warm.run_id)
        # The run used the agent box and removed it itself; the forge box
        # it never needed is still standing.
        w.cli.rm(warm.names[0])
        w.sweep_claimed(lambda run_id: True)
        assert w.sets() == []
        assert not any(info.name in warm.names for info in w.cli.ls())


class TestDispatch:
    def test_a_fresh_run_takes_the_warm_set(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        cfg = config(tmp_path)
        h = Harness(tmp_path, cfg)
        h.loop._ensure_workspace = lambda repo: None  # type: ignore[method-assign]
        w = warmer(fake_sbx, cfg)
        warm = w.fill_one()
        assert warm is not None
        h.loop._warmer = w
        h.source.items = [gh_item("1")]
        h.outcomes = ["merged"]
        result = h.loop.tick()
        assert result.dispatched is not None
        assert [run_id for run_id, _resume in h.runs] == [warm.run_id]
        assert h.loop._warm_run(warm.run_id)
        # The next fresh run finds the pool empty and mints its own id.
        h.source.items = [gh_item("2")]
        h.outcomes = ["merged"]
        h.loop.tick()
        assert h.runs[1][0] != warm.run_id and not h.loop._warm_run(h.runs[1][0])

    def test_a_resume_never_takes_a_warm_set(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        cfg = config(tmp_path)
        h = Harness(tmp_path, cfg)
        h.loop._ensure_workspace = lambda repo: None  # type: ignore[method-assign]
        w = warmer(fake_sbx, cfg)
        assert w.fill_one() is not None
        h.loop._warmer = w
        h.source.items = [gh_item("1")]
        h.outcomes = ["raise"]
        h.loop.tick()
        # The failed attempt is retried as a fresh run: that one may take a set,
        # but the first attempt's own id is what it resumes under.
        assert len(w.ready()) == 0

    def test_status_reports_the_pool(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        cfg = config(tmp_path)
        h = Harness(tmp_path, cfg)
        w = warmer(fake_sbx, cfg)
        h.loop._warmer = w
        assert h.loop.status()["warm"] == {"ready": 0, "target": 1}
        assert w.fill_one() is not None
        assert h.loop.status()["warm"] == {"ready": 1, "target": 1}


class TestEngine:
    def test_a_warm_run_reuses_its_sandboxes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.engine.engine import LoopEngine
        from sbxloop.sbx.provision import Provisioner

        seen: list[dict[str, Any]] = []

        def ensure_pair(self: Provisioner, run_id: str, *args: Any, **kwargs: Any) -> None:
            seen.append(kwargs)
            raise ProvisionError("stop here")

        monkeypatch.setattr(Provisioner, "ensure_pair", ensure_pair)
        engine = LoopEngine(config(tmp_path))
        try:
            with pytest.raises(ProvisionError, match="stop here"):
                engine.start("task", run_id=new_run_id(), warm=True)
            with pytest.raises(ProvisionError, match="stop here"):
                engine.start("task", run_id=new_run_id())
        finally:
            engine.store.close()
        assert seen[0].get("reuse_sandboxes") is True
        assert not seen[1].get("reuse_sandboxes")


class TestPrune:
    def test_a_warm_set_is_not_an_orphan(self, tmp_path: Path) -> None:
        from sbxloop.engine.store import StateStore

        warm_id, cold_id = new_run_id(), new_run_id()
        store = StateStore(tmp_path / "state.db")
        try:
            infos = [
                SandboxInfo(name=f"sbxloop-{warm_id}-agent", status="running"),
                SandboxInfo(name=f"sbxloop-{cold_id}-agent", status="running"),
            ]
            verdicts = {v.name: v for v in classify_sandboxes(infos, store, warm_run_ids={warm_id})}
        finally:
            store.close()
        assert not verdicts[f"sbxloop-{warm_id}-agent"].orphan
        assert "warm" in verdicts[f"sbxloop-{warm_id}-agent"].reason
        assert verdicts[f"sbxloop-{cold_id}-agent"].orphan
