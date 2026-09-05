"""A seeded state.db and a console wired to fakes.

The store is written through the real stores — the path the daemon takes —
so a column the daemon adds is read here the way the console will read it.
The daemon itself is a fake ctl client answering ``status``."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.control import CommandReply
from sbxloop.daemon.mailbox import MailboxClient
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import TaskSpec
from sbxloop.engine.store import StateStore
from sbxloop.tui.app import SbxloopTui
from sbxloop_worker.protocol import Event


class FakeCtl:
    """The daemon's ``status`` answer, or none (down), or stale (starting)."""

    def __init__(
        self, status: dict[str, Any] | None = None, *, down: bool = False, stale: bool = False
    ):
        self.status = status
        self.down = down
        self.stale = stale
        self.commands: list[str] = []

    def submit(self, cmd: str, *, timeout_s: float = 30.0) -> CommandReply | None:
        self.commands.append(cmd)
        if self.down:
            return None
        if self.stale:
            return CommandReply("starting", ok=False, stale=True)
        if cmd == "status":
            return CommandReply("status", status=dict(self.status or {}))
        return CommandReply(f"did {cmd}")


def live_status(**overrides: Any) -> dict[str, Any]:
    base = {
        "current": {"item_id": "gh:issue:41", "run_id": "r_live", "title": "Add retries"},
        "queued": 1,
        "runs_today": 4,
        "runs_today_resets_at": time.time() + 3600,
        "run_cap_timezone": "UTC",
        "resumes_today": 0,
        "max_runs_per_day": 12,
        "breaker_open": False,
        "consecutive_failures": 0,
        "paused": False,
        "holds": [],
        "claiming": None,
        "stopping": False,
        "pid": 4242,
        "started_at": time.time() - 7200,
        "version": "9.9.9",
        "repos": [],
    }
    base.update(overrides)
    return base


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    """A state dir with a live run, a merged run, a failed run, items, a gate."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db = state_dir / "state.db"
    store = StateStore(db)
    dstore = DaemonStore(db)
    store.create_run("r_live", "Add retries to the fetch client")
    store.save_tasks(
        "r_live",
        [
            TaskSpec(id="t1", title="Add retry policy", description="", verify_commands=["true"]),
            TaskSpec(
                id="t2",
                title="Wire client",
                description="",
                verify_commands=["true"],
                depends_on=["t1"],
            ),
        ],
    )
    store.set_run_state("r_live", "building")
    t1, t2 = store.get_tasks("r_live")
    store.update_task("r_live", t1.model_copy(update={"state": "done"}))
    store.update_task("r_live", t2.model_copy(update={"state": "executing"}))
    store.record_phase(
        "r_live",
        "build",
        task_id="t1",
        attempt=1,
        status="ok",
        output_json="{}",
        started_at=time.time() - 300,
        turns=7,
    )
    store.append_event(Event.now("run.start", "r_live", outcome="Add retries to the fetch client"))
    store.append_event(
        Event.now(
            "agent.message",
            "r_live",
            content="Reading the client first.",
            agent="builder",
            model="m",
        )
    )
    store.append_event(
        Event.now("agent.tool_start", "r_live", tool="bash", args="pytest -q", tool_call_id="c1")
    )
    store.append_event(
        Event.now(
            "agent.tool_end",
            "r_live",
            tool="bash",
            args="pytest -q",
            tool_call_id="c1",
            success=True,
            exit_code=0,
        )
    )
    store.append_event(Event.now("policy.allow", "r_live", domain="pypi.org", reason="deps"))
    store.create_run("r_done", "Bump the CI image")
    store.set_run_state("r_done", "merged")
    store.create_run("r_failed", "Rename the thing")
    store.set_run_state("r_failed", "failed")
    store.reconcile_run("r_failed", "failed", "orphaned: daemon restarted while run was in flight")
    for key, title, run in (
        ("41", "Add retries", "r_live"),
        ("40", "Bump the CI image", "r_done"),
        ("44", "Retry fetch on 5xx", None),
    ):
        item = WorkItem(
            item_id=f"gh:issue:{key}",
            source_key=key,
            title=title,
            url=f"https://x/issues/{key}",
            repo="o/r",
        )
        dstore.upsert_new(item, now=1.0)
        if run:
            dstore.mark_running(item.item_id, run, now=2.0)
    dstore.finish_ledger("r_done", "done", now=3.0)
    dstore.mark_done("gh:issue:40", now=3.0)
    dstore.create_merge_gate(
        "r_done", "gh:issue:40", "o/r", 170, "https://x/pull/170", None, ["brett"], "tok", 4.0
    )
    dstore.set_local_heartbeat(time.time())
    dstore.close()
    store.close()
    return state_dir


def make_app(
    state_dir: Path, *, ctl: FakeCtl | None = None, run: str | None = None, **tui: Any
) -> SbxloopTui:
    config = Config.model_validate({"state_dir": str(state_dir), "tui": tui})
    mailbox = MailboxClient(state_dir / "state.db", operator_id="brett")
    return SbxloopTui(
        config, state_dir, mailbox=mailbox, ctl=ctl or FakeCtl(live_status()), initial_run=run
    )


def drive(coro_fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run one async console scenario from a synchronous test."""
    asyncio.run(coro_fn())


__all__ = ["FakeCtl", "drive", "live_status", "make_app"]
