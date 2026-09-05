"""MailboxClient: the console's handle on state.db — read-only for the
daemon's and the engine's state, write-only for inbound mailbox rows, and
never a schema statement."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from sbxloop.daemon.mailbox import MailboxClient
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.store import StateStore
from sbxloop.errors import DaemonError
from sbxloop_worker.protocol import Event


def stores(tmp_path: Path) -> tuple[Path, DaemonStore, StateStore]:
    path = tmp_path / "state.db"
    dstore = DaemonStore(path)
    store = StateStore(path)
    return path, dstore, store


def test_refuses_a_missing_or_mailbox_less_store(tmp_path: Path) -> None:
    with pytest.raises(DaemonError, match="does not exist"):
        MailboxClient(tmp_path / "none.db", operator_id="b")
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).execute("CREATE TABLE runs (run_id TEXT)").connection.commit()
    with pytest.raises(DaemonError, match="predates the operator console"):
        MailboxClient(bare, operator_id="b")


def test_opens_without_ddl_while_the_daemon_is_closed(tmp_path: Path) -> None:
    """A WAL store with no daemon on it (the -shm gone) still opens
    read-only; the mailbox writes through its own connection."""
    path, dstore, store = stores(tmp_path)
    dstore.close()
    store.close()
    schema_before = sorted(
        r[0] for r in sqlite3.connect(path).execute("SELECT sql FROM sqlite_master WHERE sql")
    )
    client = MailboxClient(path, operator_id="brett")
    assert client.daemon_alive(now=time.time()) is False
    assert client.runs() == [] and client.items() == []
    rid = client.post("control", "!sbx status", now=5.0)
    assert client.message(rid) is not None
    client.close()
    schema_after = sorted(
        r[0] for r in sqlite3.connect(path).execute("SELECT sql FROM sqlite_master WHERE sql")
    )
    assert schema_after == schema_before


def test_reads_match_the_stores_views(tmp_path: Path) -> None:
    path, dstore, store = stores(tmp_path)
    store.create_run("r1", "Do A")
    store.append_event(Event.now("run.start", "r1", outcome="Do A"))
    store.append_event(Event.now("agent.message", "r1", content="hi"))
    dstore.upsert_new(
        WorkItem(item_id="gh:issue:5", source_key="5", title="Five", url="u", body=""), now=1.0
    )
    dstore.mark_running("gh:issue:5", "r1", now=2.0)
    dstore.create_merge_gate("r1", "gh:issue:5", "o/r", 9, "u", None, ["b"], "tok", 3.0)
    dstore.set_local_heartbeat(100.0)
    client = MailboxClient(path, operator_id="brett")
    assert [r.run_id for r in client.runs()] == ["r1"]
    assert client.run("r1") is not None and client.run("nope") is None
    assert client.tasks("r1") == store.get_tasks("r1")
    assert [(seq, e.type) for seq, e in client.events("r1")] == [
        (1, "run.start"),
        (2, "agent.message"),
    ]
    assert [e.type for _, e in client.events("r1", after_seq=1)] == ["agent.message"]
    assert [e.type for _, e in client.events("r1", type_prefix="run.")] == ["run.start"]
    assert client.last_event_ts("r1") is not None
    assert [i.item_id for i in client.items()] == ["gh:issue:5"]
    assert client.item("gh:issue:5") is not None
    assert client.item_for_run("r1") == "gh:issue:5"
    assert [g.run_id for g in client.gates()] == ["r1"]
    assert client.gate_open("r1") and not client.gate_open("r2")
    assert client.holds() == []
    assert client.daemon_alive(now=104.0) and not client.daemon_alive(now=200.0)
    snap = client.status_snapshot(now=104.0)
    assert snap["alive"] and snap["current"] == {"run_id": "r1", "item_id": "gh:issue:5"}
    assert snap["gates"] == 1 and snap["queued"] == 0
    client.close()


def test_inbound_rows_are_what_the_bridge_takes(tmp_path: Path) -> None:
    path, dstore, _ = stores(tmp_path)
    q = dstore.local_post("control", "which?", now=1.0, kind="choices", choices_json="{}")
    p = dstore.local_post("thread:1", "⏸ prompt", now=1.0, kind="gate", gate_run_id="r1")
    client = MailboxClient(path, operator_id="brett", operator_name="Brett")
    a = client.post("control", "@sbx hi", now=2.0)
    b = client.click_choice(q, "layout", now=3.0)
    c = client.approve(p, now=4.0)
    assert client.taken([a, b, c]) == set()
    rows = dstore.take_local_inbound(now=5.0)
    assert [(r.id, r.kind, r.text, r.channel_id, r.reply_to_id) for r in rows] == [
        (a, "message", "@sbx hi", "control", None),
        (b, "choice", "layout", "control", q),
        (c, "approve", "", "thread:1", p),
    ]
    assert rows[0].author_id == "brett" and rows[0].author_name == "Brett"
    assert rows[1].reply_to_direction == "out"
    assert client.taken([a, b, c]) == {a, b, c}
    assert [m.id for m in client.messages("control")] == [q, a, b]
    assert [m.id for m in client.messages("control", after_id=a)] == [b]
    assert client.threads() == []
    dstore.record_chat_thread("r1", "control", "thread:7", "7", backend="local")
    assert [run for run, _ in client.threads()] == ["r1"]
    thread = client.thread_for_run("r1")
    assert thread is not None and thread.thread_id == "thread:7"
    dstore.local_edit(q, "which? (edited)", now=9.0)
    assert [m.id for m in client.changed_since("control", after_id=b, since=8.0)] == [q]
    # A reaction, a claim and a resolved gate are changes too, though no
    # text moved: the console repaints reactions and buttons from them.
    dstore.local_react(a, "⏳", now=10.0)
    assert [m.id for m in client.changed_since("control", after_id=b, since=9.5)] == [a]
    dstore.local_clear_gate(p, now=11.0)
    assert [m.id for m in client.changed_since("thread:1", after_id=p, since=10.5)] == [p]
    assert client.latest_ids() == {"control": b, "thread:1": c}
    client.close()


def test_a_state_dir_with_uri_characters_still_opens(tmp_path: Path) -> None:
    odd = tmp_path / "50%off#1?x"
    odd.mkdir()
    dstore = DaemonStore(odd / "state.db")
    StateStore(odd / "state.db").close()
    dstore.close()
    client = MailboxClient(odd / "state.db", operator_id="b")
    assert client.runs() == []
    client.close()


def test_reads_are_serialised_across_threads(tmp_path: Path) -> None:
    """The console reads from several worker threads; one connection
    tolerates one caller at a time, so the client locks every call."""
    import threading

    path, _dstore, store = stores(tmp_path)
    for n in range(50):
        store.create_run(f"r{n}", "x")
        store.append_event(Event.now("run.start", f"r{n}"))
    client = MailboxClient(path, operator_id="b")
    errors: list[BaseException] = []

    def hammer() -> None:
        try:
            for _ in range(30):
                client.runs()
                client.events("r1")
                client.items()
                client.messages("control")
        except BaseException as exc:  # pragma: no cover - the failure we guard
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    client.close()
