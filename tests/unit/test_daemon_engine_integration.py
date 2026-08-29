"""DaemonLoop driving the REAL LoopEngine (echo backend, fake sbx) for one
issue — proves the shared-store / fresh-bus / default-runner wiring against
the actual engine, not a stand-in. No repository is configured, so the
engine ends `completed` after its gate and the daemon settles that as
done; the pipeline's GitHub stages are covered by the engine tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.store import StateStore
from sbxloop.sbx.cli import SbxCLI
from tests.conftest import FakeSbx
from tests.unit.test_daemon_loop import FakeSource, gh_item
from tests.unit.test_engine import BUILD, Harness, task, taskgraph


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


def test_an_issue_runs_through_the_real_engine(harness: Harness, tmp_path: Path) -> None:
    harness.script([taskgraph(task("t1")), BUILD])
    config = Config.model_validate(
        {
            "state_dir": str(harness.state_dir),
            "limits": {"disk_warn": 0, "disk_abort": 0, "mem_warn": 0},
        }
    )
    store = StateStore(harness.state_dir / "state.db")
    dstore = DaemonStore(harness.state_dir / "state.db")
    source = FakeSource([gh_item("7", title="Add a flag", body="--verbose please")])

    loop = DaemonLoop(
        config,
        store=store,
        dstore=dstore,
        source=source,
        sbx=SbxCLI(binary=str(harness.fake_sbx.binary)),
        worker_python=sys.executable,
        install_workers=False,
    )
    result = loop.tick()

    assert result.dispatched == "gh:issue:7"
    assert result.outcome == "done", result
    item = dstore.get("gh:issue:7")
    assert item is not None and item.state == "done" and item.run_id is not None
    # the run exists in the shared engine store, completed, with the
    # daemon-built outcome text carrying the provenance trailer
    run = store.get_run(item.run_id)
    assert run.state == "completed"
    assert "GitHub issue #7" in run.outcome
    assert run.outcome.startswith("Add a flag\n\n--verbose please")
    # source-side bookkeeping landed
    assert [c[0] for c in source.calls] == ["claim", "started", "merged"]
    # sandboxes cleaned up (keep_on_failure forced off; run succeeded)
    assert harness.sandboxes_left() == []
