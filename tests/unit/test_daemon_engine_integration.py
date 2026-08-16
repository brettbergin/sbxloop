"""DaemonLoop driving the REAL LoopEngine (echo backend, fake sbx) for one
inbox item — proves the shared-store / fresh-bus / default-runner wiring
against the actual engine, not a stand-in."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.sources import InboxSource
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.store import StateStore
from sbxloop.sbx.cli import SbxCLI
from tests.conftest import FakeSbx
from tests.unit.test_engine import ACCEPT, EXECUTE, PASS, PLAN, Harness, task, taskgraph


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


def test_inbox_item_runs_through_the_real_engine(harness: Harness, tmp_path: Path) -> None:
    harness.script([taskgraph(task("t1")), PLAN, EXECUTE, PASS, ACCEPT])
    config = Config.model_validate(
        {
            "state_dir": str(harness.state_dir),
            "limits": {"disk_warn": 0, "disk_abort": 0, "mem_warn": 0},
            "daemon": {"inbox_dir": str(tmp_path / "inbox")},
        }
    )
    store = StateStore(harness.state_dir / "state.db")
    dstore = DaemonStore(harness.state_dir / "state.db")
    # A clock far in the future makes the just-written file count as settled.
    inbox = InboxSource(tmp_path / "inbox", clock=lambda: time.time() + 60.0)
    (tmp_path / "inbox" / "pending" / "add-flag.md").write_text(
        "# Add a flag\n\n--verbose please\n"
    )

    loop = DaemonLoop(
        config,
        store=store,
        dstore=dstore,
        sources=[inbox],
        sbx=SbxCLI(binary=str(harness.fake_sbx.binary)),
        worker_python=sys.executable,
        install_workers=False,
    )
    result = loop.tick()

    assert result.dispatched == "inbox:add-flag.md"
    assert result.outcome == "done", result
    item = dstore.get("inbox:add-flag.md")
    assert item is not None and item.state == "done" and item.run_id is not None
    # the run exists in the shared engine store, completed, with the
    # daemon-built outcome text carrying the source trailer
    run = store.get_run(item.run_id)
    assert run.state == "completed"
    assert "inbox file `add-flag.md`" in run.outcome
    assert run.outcome.startswith("Add a flag\n\n--verbose please")
    # source-side bookkeeping landed
    assert (tmp_path / "inbox" / "done" / "add-flag.md").exists()
    assert "completed" in (tmp_path / "inbox" / "done" / "add-flag.result.md").read_text()
    # sandboxes cleaned up (keep_on_failure forced off; run succeeded)
    assert harness.sandboxes_left() == []
