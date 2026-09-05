"""A seeded state.db and a console wired to fakes.

The store is written through the real stores — the path the daemon takes —
so a column the daemon adds is read here the way the console will read it.
The daemon itself is a fake ctl client answering ``status``."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable, Coroutine, Iterator, Sequence
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
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import ExecResult, SandboxInfo
from sbxloop.tui.app import SbxloopTui
from sbxloop.tui.runner import RunOutcome
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


class FakeChild:
    """A process the fake runner "started"."""

    _next_pid = 5000

    def __init__(self) -> None:
        FakeChild._next_pid += 1
        self.pid = FakeChild._next_pid
        self.code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = -15

    def wait(self, timeout_s: float) -> int | None:
        return self.code


class FakeStream:
    """Scripted lines, then a tail that blocks until closed (`-f`)."""

    def __init__(self, lines: Sequence[str]) -> None:
        self._lines = list(lines)
        self.closed = threading.Event()

    def lines(self) -> Iterator[str]:
        yield from self._lines
        self.closed.wait()

    def close(self) -> None:
        self.closed.set()


class FakeRunner:
    """Records every argv; answers from scripts keyed by an argv prefix."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.scripts: dict[tuple[str, ...], RunOutcome] = {}
        self.stream_lines: list[str] = []
        self.streams: list[FakeStream] = []
        self.spawned: list[tuple[tuple[str, ...], Path | None, Path | None]] = []
        self.children: list[FakeChild] = []
        self.interactive_calls: list[tuple[str, ...]] = []

    def script(self, *prefix: str, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.scripts[prefix] = RunOutcome(prefix, returncode, stdout, stderr)

    def run(self, argv: Sequence[str], *, timeout_s: float = 60.0) -> RunOutcome:
        argv = tuple(argv)
        self.calls.append(argv)
        for prefix in sorted(self.scripts, key=len, reverse=True):
            if argv[: len(prefix)] == prefix:
                out = self.scripts[prefix]
                return RunOutcome(argv, out.returncode, out.stdout, out.stderr)
        return RunOutcome(argv, 127, stderr=f"{argv[0]}: not found on PATH")

    def spawn(
        self, argv: Sequence[str], *, cwd: Path | None = None, log_path: Path | None = None
    ) -> FakeChild:
        child = FakeChild()
        self.spawned.append((tuple(argv), cwd, log_path))
        self.children.append(child)
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("")
        return child

    def stream(self, argv: Sequence[str]) -> FakeStream:
        stream = FakeStream(self.stream_lines)
        self.streams.append(stream)
        return stream

    def interactive(self, argv: Sequence[str]) -> int:
        self.interactive_calls.append(tuple(argv))
        return 0


class RecordingSbx(SbxCLI):
    """An sbx CLI that lists what it is told and records every call."""

    def __init__(self, infos: Sequence[SandboxInfo] = ()) -> None:
        super().__init__(binary="sbx")
        self.infos = list(infos)
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, **kwargs: Any) -> ExecResult:
        self.calls.append(tuple(args))
        if args[:1] == ("rm",):
            name = args[-1]
            self.infos = [i for i in self.infos if i.name != name]
        return ExecResult(argv=list(args), returncode=0, stdout="", stderr="", duration_s=0.0)

    def ls(self) -> list[SandboxInfo]:
        self.calls.append(("ls",))
        return list(self.infos)


def backdate(state_dir: Path, run_id: str, days: float) -> None:
    """Make a run look untouched for ``days`` (age drives orphan and gc verdicts)."""
    conn = sqlite3.connect(state_dir / "state.db")
    try:
        conn.execute(
            "UPDATE runs SET updated_at = ? WHERE run_id = ?",
            (time.time() - days * 86400, run_id),
        )
        conn.commit()
    finally:
        conn.close()


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
    state_dir: Path,
    *,
    ctl: FakeCtl | None = None,
    run: str | None = None,
    runner: FakeRunner | None = None,
    sbx: RecordingSbx | None = None,
    read_only: bool = False,
    daemon: dict[str, Any] | None = None,
    **tui: Any,
) -> SbxloopTui:
    config = Config.model_validate(
        {"state_dir": str(state_dir), "tui": tui, "daemon": daemon or {}}
    )
    mailbox = MailboxClient(state_dir / "state.db", operator_id="brett")
    box = sbx or RecordingSbx()
    return SbxloopTui(
        config,
        state_dir,
        mailbox=mailbox,
        ctl=ctl or FakeCtl(live_status()),
        initial_run=run,
        runner=runner or FakeRunner(),
        sbx_factory=lambda: box,
        read_only=read_only,
        cwd=state_dir,
    )


def drive(coro_fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run one async console scenario from a synchronous test."""
    asyncio.run(coro_fn())


__all__ = [
    "FakeChild",
    "FakeCtl",
    "FakeRunner",
    "FakeStream",
    "RecordingSbx",
    "backdate",
    "drive",
    "live_status",
    "make_app",
]
