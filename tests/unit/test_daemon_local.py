"""The local chat bridge: the operator console's transport is a mailbox in
state.db, and everything Discord shows — headline, thread, edits in place,
reactions, steering, the concierge, choices, the merge gate — lands there
as rows the console renders and answers."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.concierge import ConciergeReply
from sbxloop.daemon.local import (
    EXPIRED_CLICK_NOTE,
    LOCAL_BOT_ID,
    STALE_INBOUND_NOTE,
    LocalBridge,
)
from sbxloop.daemon.model import DaemonNotice, RunReport, WorkItem
from sbxloop.daemon.store import LOCAL_HEARTBEAT_KEY, LOCAL_STARTED_KEY, DaemonStore
from sbxloop.events import EventBus
from tests.unit.test_daemon_discord import FakeConcierge, FakeEngine, FakeLoop, wait_for


def make_bridge(
    tmp_path: Path, *, concierge: Any = None, **tui: Any
) -> tuple[LocalBridge, DaemonStore, FakeLoop]:
    """A headless daemon's bridge: no [chat] backend at all."""
    config = Config.model_validate({"home": str(tmp_path / "state"), "tui": tui})
    dstore = DaemonStore(config.paths.state_db)
    floop = FakeLoop(dstore)
    bridge = LocalBridge(config, dstore, loop_ref=floop, concierge=concierge)
    return bridge, dstore, floop


def rows(dstore: DaemonStore, channel: str) -> list[Any]:
    return dstore.local_messages(channel)


def texts(dstore: DaemonStore, channel: str) -> list[str]:
    return [r.text for r in rows(dstore, channel) if r.direction == "out"]


def typed(dstore: DaemonStore, channel: str, text: str, **fields: Any) -> int:
    """What the console writes when an operator types."""
    return dstore.local_post(
        channel,
        text,
        now=time.time(),
        direction="in",
        author_id="brett",
        author_name="brett",
        **fields,
    )


def thread_channel(dstore: DaemonStore, run_id: str) -> str:
    known = dstore.chat_thread(run_id, "local")
    assert known is not None
    return known.thread_id


class TestConstruction:
    def test_no_credentials_and_always_enabled(self, tmp_path: Path) -> None:
        bridge, _, _ = make_bridge(tmp_path)
        assert bridge.backend == "local"
        assert bridge.chat.enabled is True
        assert bridge.chat.channel_ref == "control"
        assert bridge._bot_user_id() == LOCAL_BOT_ID
        assert bridge.mention_user("brett") == "@brett"

    def test_start_stamps_liveness_and_stops_cleanly(self, tmp_path: Path) -> None:
        bridge, dstore, _ = make_bridge(tmp_path)
        bridge.start(connect_wait_s=2)
        try:
            assert wait_for(lambda: dstore.get_value(LOCAL_HEARTBEAT_KEY) is not None)
            assert dstore.get_value(LOCAL_STARTED_KEY) is not None
        finally:
            bridge.close(drain_wait_s=1)
        assert bridge._thread is not None and not bridge._thread.is_alive()


class TestRunThread:
    def test_headline_thread_chronology_and_finish_land_as_rows(self, tmp_path: Path) -> None:
        bridge, dstore, _ = make_bridge(tmp_path)
        bridge.start(connect_wait_s=2)
        try:
            item = WorkItem(item_id="gh:issue:7", source_key="7", title="Do A")
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            assert wait_for(lambda: dstore.chat_thread("r1", "local") is not None)
            known = dstore.chat_thread("r1", "local")
            assert known is not None
            assert known.channel_id == "control"
            assert known.thread_id == f"thread:{known.headline_id}"
            control = rows(dstore, "control")
            headline = next(r for r in control if str(r.id) == known.headline_id)
            assert headline.text.startswith("▶ run `r1`")
            assert headline.embed_json is not None
            assert json.loads(headline.embed_json)["fields"]
            bus.emit("agent.message", "r1", content="Planning now", agent="planner", model="m")
            bus.emit("run.deliver", "r1", repo="o/r", pr=3, url="https://x/pull/3")
            thread = known.thread_id
            assert wait_for(lambda: any("planner" in s for s in texts(dstore, thread)))
            assert wait_for(lambda: any("PR [#3 · o/r]" in s for s in texts(dstore, thread)))
            bridge.run_finished(
                item, RunReport("r1", "completed", "1/1 tasks done", pr=(3, "https://x/pull/3"))
            )
            assert wait_for(
                lambda: any(s.startswith("**finished: completed**") for s in texts(dstore, thread))
            )
            # The headline is edited in place, never re-posted.
            assert wait_for(lambda: dstore.local_message(headline.id).text.startswith("✅"))  # type: ignore[union-attr]
            assert dstore.local_message(headline.id).edited_at is not None  # type: ignore[union-attr]
            assert sum(1 for r in rows(dstore, "control") if r.text.startswith(("▶", "✅"))) == 1
        finally:
            bridge.close(drain_wait_s=1)

    def test_status_line_is_one_row_edited_in_place(self, tmp_path: Path) -> None:
        bridge, dstore, _ = make_bridge(tmp_path)
        bridge.start(connect_wait_s=2)
        try:
            item = WorkItem(item_id="gh:issue:7", source_key="7", title="Do A")
            bus = EventBus()
            bridge.run_started(item, "r1", FakeEngine(), bus)  # type: ignore[arg-type]
            assert wait_for(lambda: dstore.chat_thread("r1", "local") is not None)
            thread = thread_channel(dstore, "r1")
            bus.emit(
                "run.tasks",
                "r1",
                tasks=[{"id": "t1", "title": "One", "state": "pending", "depends_on": []}],
            )
            bus.emit("task.start", "r1", task_id="t1", title="One", state="executing")
            assert wait_for(
                lambda: (dstore.chat_thread("r1", "local") or (None,) * 5)[3] is not None, 8
            )
            status_id = dstore.chat_thread("r1", "local").status_id  # type: ignore[union-attr]
            assert status_id is not None
            first = dstore.local_message(int(status_id))
            assert first is not None and "task 1/1" in first.text
            bus.emit("phase.end", "r1", task_id="t1", phase="verify", status="ok")
            assert wait_for(
                lambda: "verify" in (dstore.local_message(int(status_id)) or first).text, 8
            )
            assert [r.id for r in rows(dstore, thread) if "task 1/1" in r.text] == [int(status_id)]
        finally:
            bridge.close(drain_wait_s=1)


class TestInbound:
    def test_command_in_control_answers_with_a_card(self, tmp_path: Path) -> None:
        bridge, dstore, _ = make_bridge(tmp_path)
        bridge.start(connect_wait_s=2)
        try:
            asked = typed(dstore, "control", "!sbx status")
            assert wait_for(
                lambda: any(r.direction == "out" and r.id > asked for r in rows(dstore, "control"))
            )
            reply = next(r for r in rows(dstore, "control") if r.direction == "out")
            assert reply.embed_json is not None
            assert "queued" in reply.text or "queued" in reply.embed_json
            taken = dstore.local_message(asked)
            assert taken is not None and taken.taken_at is not None
        finally:
            bridge.close(drain_wait_s=1)

    def test_mention_in_control_is_a_concierge_turn(self, tmp_path: Path) -> None:
        concierge = FakeConcierge([ConciergeReply("two runs today; `r1` is live")])
        concierge.tool_calls = [("sbx_control", {"command": "status"}, True)]
        bridge, dstore, _ = make_bridge(tmp_path, concierge=concierge)
        bridge.start(connect_wait_s=2)
        try:
            asked = typed(dstore, "control", "@sbx what's running?")
            assert wait_for(lambda: any("two runs today" in s for s in texts(dstore, "control")))
            assert concierge.turns == [("what's running?", "TUI user `brett`")]
            assert concierge.author_ids == ["brett"] and concierge.vias == ["local"]
            row = dstore.local_message(asked)
            assert row is not None and "⏳" in row.reactions and "✅" in row.reactions
            assert any(s.startswith("🛠 concierge: sbx_control") for s in texts(dstore, "control"))
        finally:
            bridge.close(drain_wait_s=1)

    def test_plain_text_in_control_is_left_alone(self, tmp_path: Path) -> None:
        concierge = FakeConcierge()
        bridge, dstore, _ = make_bridge(tmp_path, concierge=concierge)
        bridge.start(connect_wait_s=2)
        try:
            asked = typed(dstore, "control", "talking to a colleague here")
            assert wait_for(lambda: (dstore.local_message(asked) or asked).taken_at is not None)  # type: ignore[union-attr]
            time.sleep(0.3)
            assert concierge.turns == []
            assert texts(dstore, "control") == []
        finally:
            bridge.close(drain_wait_s=1)

    def test_reply_to_a_bot_row_is_addressed(self, tmp_path: Path) -> None:
        concierge = FakeConcierge([ConciergeReply("sure")])
        bridge, dstore, _ = make_bridge(tmp_path, concierge=concierge)
        bridge.start(connect_wait_s=2)
        try:
            bridge.daemon_notice(DaemonNotice(kind="daemon.started", text="daemon up"))
            assert wait_for(lambda: any("daemon up" in s for s in texts(dstore, "control")))
            notice = next(r for r in rows(dstore, "control") if "daemon up" in r.text)
            typed(dstore, "control", "and why?", reply_to_id=notice.id)
            assert wait_for(lambda: concierge.turns == [("and why?", "TUI user `brett`")])
        finally:
            bridge.close(drain_wait_s=1)

    def test_steer_in_thread_needs_the_mention(self, tmp_path: Path) -> None:
        bridge, dstore, _ = make_bridge(tmp_path)
        bridge.start(connect_wait_s=2)
        try:
            item = WorkItem(item_id="gh:issue:7", source_key="7", title="Do A")
            engine = FakeEngine()
            bus = EventBus()
            bridge.run_started(item, "r1", engine, bus)  # type: ignore[arg-type]
            assert wait_for(lambda: dstore.chat_thread("r1", "local") is not None)
            thread = thread_channel(dstore, "r1")
            plain = typed(dstore, thread, "just chatting")
            assert wait_for(lambda: (dstore.local_message(plain) or plain).taken_at is not None)  # type: ignore[union-attr]
            assert engine.posted == []
            steer = typed(dstore, thread, "@sbx focus on auth first")
            assert wait_for(lambda: engine.posted == ["focus on auth first"])
            assert wait_for(lambda: "⏳" in (dstore.local_message(steer) or plain).reactions)  # type: ignore[union-attr]
            bus.emit("chat.reply", "r1", message_id="m1", reply="Will do.", action="steer_task")
            assert wait_for(lambda: "✅" in (dstore.local_message(steer) or plain).reactions)  # type: ignore[union-attr]
            assert wait_for(lambda: any("Will do." in s for s in texts(dstore, thread)))
        finally:
            bridge.close(drain_wait_s=1)

    def test_a_row_typed_before_the_daemon_started_is_refused(self, tmp_path: Path) -> None:
        """Pending at boot, or stamped before the bridge was built: refused
        with a note, never executed — the ctl queue's rule."""
        bridge, dstore, floop = make_bridge(tmp_path)
        stale = dstore.local_post(
            "control", "!sbx cancel", now=time.time() + 60, direction="in", author_id="brett"
        )
        bridge.start(connect_wait_s=2)
        try:
            assert wait_for(lambda: any(r.reply_to_id == stale for r in rows(dstore, "control")))
            note = next(r for r in rows(dstore, "control") if r.reply_to_id == stale)
            assert note.text == STALE_INBOUND_NOTE
            assert floop.cancelled == 0
        finally:
            bridge.close(drain_wait_s=1)


class TestChoices:
    def test_question_carries_choices_and_a_click_answers_it(self, tmp_path: Path) -> None:
        question = (
            "Is this about the wording, the layout, or the timing?\n"
            "```sbx-choices\n"
            '{"prompt": "Which one?", "choices": [{"value": "layout", "label": "Layout"}, '
            '{"value": "timing", "label": "Timing"}]}\n'
            "```"
        )
        from sbxloop.daemon.chat_choices import parse_choice_question

        prose, spec = parse_choice_question(question)
        assert spec is not None
        concierge = FakeConcierge(
            [ConciergeReply(prose, question=spec), ConciergeReply("filed as layout")]
        )
        bridge, dstore, _ = make_bridge(tmp_path, concierge=concierge)
        bridge.start(connect_wait_s=2)
        try:
            typed(dstore, "control", "@sbx the spinner never stops")
            assert wait_for(lambda: any(r.kind == "choices" for r in rows(dstore, "control")))
            q = next(r for r in rows(dstore, "control") if r.kind == "choices")
            assert q.choices_json is not None
            data = json.loads(q.choices_json)
            assert [c["value"] for c in data["choices"]] == ["layout", "timing"]
            assert data["answered"] is None and data["expires_at"] > time.time()
            assert "1." in q.text and "Layout" in q.text
            typed(dstore, "control", "layout", kind="choice", reply_to_id=q.id)
            assert wait_for(lambda: len(concierge.turns) == 2)
            assert concierge.turns[1][0] == "layout"
            assert wait_for(
                lambda: (
                    json.loads(dstore.local_message(q.id).choices_json)["answered"]  # type: ignore[union-attr, arg-type]
                    is not None
                )
            )
            answered = dstore.local_message(q.id)
            assert answered is not None and "_Answered: **layout**" in answered.text
            assert wait_for(lambda: any("filed as layout" in s for s in texts(dstore, "control")))
        finally:
            bridge.close(drain_wait_s=1)

    def test_a_click_on_an_unknown_question_gets_the_typed_route(self, tmp_path: Path) -> None:
        bridge, dstore, _ = make_bridge(tmp_path, concierge=FakeConcierge())
        bridge.start(connect_wait_s=2)
        try:
            click = typed(dstore, "control", "layout", kind="choice", reply_to_id=999)
            assert wait_for(lambda: any(r.reply_to_id == click for r in rows(dstore, "control")))
            note = next(r for r in rows(dstore, "control") if r.reply_to_id == click)
            assert note.text == EXPIRED_CLICK_NOTE
        finally:
            bridge.close(drain_wait_s=1)


class TestMergeGate:
    def _gate(self, dstore: DaemonStore, run_id: str = "r77") -> Any:
        dstore.create_merge_gate(
            run_id, "gh:issue:7", "o/r", 9, "https://x/pull/9", None, ["brett"], "tok77", 1.0
        )
        return dstore.merge_gate_for(run_id)

    def test_prompt_row_offers_approval_until_finalised(self, tmp_path: Path) -> None:
        bridge, dstore, floop = make_bridge(tmp_path)
        floop.approve_merge = lambda run_id, by=None: f"approved {run_id} by {by}"  # type: ignore[attr-defined]
        bridge.start(connect_wait_s=2)
        try:
            item = WorkItem(item_id="gh:issue:7", source_key="7", title="Seven")
            bridge.run_started(item, "r77", FakeEngine(), EventBus())  # type: ignore[arg-type]
            assert wait_for(lambda: dstore.chat_thread("r77", "local") is not None)
            thread = thread_channel(dstore, "r77")
            gate = self._gate(dstore)
            bridge.merge_gate_opened(item, "r77", gate)
            assert wait_for(lambda: any(r.kind == "gate" for r in rows(dstore, thread)))
            prompt = next(r for r in rows(dstore, thread) if r.kind == "gate")
            assert prompt.gate_run_id == "r77" and prompt.mention_users
            assert "@brett" in prompt.text and "!sbx merge gh:issue:7" in prompt.text
            assert dstore.gate_prompt("r77", "local") == (thread, str(prompt.id))
            # The console's approve button is an `approve` row under the prompt.
            typed(dstore, thread, "", kind="approve", reply_to_id=prompt.id)
            assert wait_for(
                lambda: any("approved r77 by TUI user `brett`" in s for s in texts(dstore, thread))
            )
            bridge.merge_gate_resolved(item, "r77", gate, "merged", "brett", "abc123def456")
            assert wait_for(lambda: (dstore.local_message(prompt.id) or prompt).gate_run_id is None)  # type: ignore[union-attr]
            final = dstore.local_message(prompt.id)
            assert final is not None and "approved by brett" in final.text
        finally:
            bridge.close(drain_wait_s=1)

    def test_a_standing_gate_is_reposted_after_a_restart(self, tmp_path: Path) -> None:
        bridge, dstore, _ = make_bridge(tmp_path)
        dstore.record_chat_thread("r77", "control", "thread:1", "1", backend="local")
        self._gate(dstore)
        bridge.start(connect_wait_s=2)
        try:
            assert wait_for(lambda: dstore.gate_prompt("r77", "local") is not None)
            where = dstore.gate_prompt("r77", "local")
            assert where is not None and where[0] == "thread:1"
        finally:
            bridge.close(drain_wait_s=1)

    def test_prune_keeps_an_open_gates_prompt_and_a_threads_anchors(self, tmp_path: Path) -> None:
        bridge, dstore, _ = make_bridge(tmp_path, retention_days=1)
        self._gate(dstore)
        old = time.time() - 5 * 86400
        kept = dstore.local_post("thread:1", "⏸ prompt", now=old, kind="gate", gate_run_id="r77")
        headline = dstore.local_post("control", "▶ run", now=old)
        status = dstore.local_post(f"thread:{headline}", "⏳ task 1/1", now=old)
        dstore.record_chat_thread(
            "r_old", "control", f"thread:{headline}", str(headline), backend="local"
        )
        dstore.set_chat_status_id("r_old", str(status), backend="local")
        dropped = dstore.local_post("thread:1", "old chatter", now=old)
        bridge._prune(time.time())
        assert dstore.local_message(kept) is not None
        assert dstore.local_message(headline) is not None, "the thread's headline stays"
        assert dstore.local_message(status) is not None, "and its status line"
        assert dstore.local_message(dropped) is None


class TestWatches:
    def test_foreign_ids_are_not_the_consoles_to_ping(self, tmp_path: Path) -> None:
        """A Discord snowflake or a Slack member id on a work item is
        another bridge's; the console neither watches nor mentions it."""
        bridge, dstore, _ = make_bridge(tmp_path)
        assert bridge._owns_user_id("brett") and bridge._owns_user_id("ops-2")
        assert not bridge._owns_user_id("123456789012345678")
        assert not bridge._owns_user_id("U0123ABCDEF")
        bridge.start(connect_wait_s=2)
        try:
            item = WorkItem(
                item_id="gh:issue:7",
                source_key="7",
                title="Seven",
                requested_by="123456789012345678",
            )
            bridge.run_started(item, "r1", FakeEngine(), EventBus())  # type: ignore[arg-type]
            assert wait_for(lambda: dstore.chat_thread("r1", "local") is not None)
            assert dstore.all_run_watches("local") == {}, "not the console's watcher"
            thread = thread_channel(dstore, "r1")
            bridge.run_finished(item, RunReport("r1", "completed", "1/1 tasks done"))
            assert wait_for(lambda: any("finished" in s for s in texts(dstore, thread)))
            everything = texts(dstore, "control") + texts(dstore, thread)
            assert not any("@1234567890" in s for s in everything)
        finally:
            bridge.close(drain_wait_s=1)

    def test_watch_notice_pings_the_operator_by_name(self, tmp_path: Path) -> None:
        bridge, dstore, _ = make_bridge(tmp_path)
        bridge.start(connect_wait_s=2)
        try:
            item = WorkItem(item_id="gh:issue:7", source_key="7", title="Seven")
            bridge.run_started(item, "r1", FakeEngine(), EventBus())  # type: ignore[arg-type]
            bridge._remember_requester("TUI user `brett`", "brett")
            assert bridge.on_watch("r1", "TUI user `brett`") is None
            assert dstore.all_run_watches("local") == {"r1": ["brett"]}
            bridge.run_finished(item, RunReport("r1", "completed", "1/1 tasks done"))
            assert wait_for(lambda: any("@brett" in s for s in texts(dstore, "control")))
        finally:
            bridge.close(drain_wait_s=1)


@pytest.mark.parametrize("embeds", [True, False])
def test_cards_follow_the_embeds_knob(tmp_path: Path, embeds: bool) -> None:
    bridge, dstore, _ = make_bridge(tmp_path, embeds=embeds)
    bridge.start(connect_wait_s=2)
    try:
        typed(dstore, "control", "!sbx status")
        assert wait_for(lambda: any(r.direction == "out" for r in rows(dstore, "control")))
        reply = next(r for r in rows(dstore, "control") if r.direction == "out")
        assert (reply.embed_json is not None) is embeds
        if not embeds:
            assert "queued" in reply.text
    finally:
        bridge.close(drain_wait_s=1)


def test_a_result_with_files_names_them_by_host_path(tmp_path: Path) -> None:
    """#799: the console runs on the daemon host, so a path is the file —
    the local bridge names every file the result carried."""
    import asyncio
    import types

    bridge, dstore, _ = make_bridge(tmp_path)
    target = types.SimpleNamespace(id="thread:1")
    asyncio.run(
        bridge._send(
            target, "📣 result", files=["/state/runs/r1/artifacts/a.csv", "/state/runs/r1/b.png"]
        )
    )
    (text,) = texts(dstore, "thread:1")
    assert text == (
        "📣 result\n"
        "📎 `a.csv` — on the daemon host at `/state/runs/r1/artifacts/a.csv`\n"
        "📎 `b.png` — on the daemon host at `/state/runs/r1/b.png`"
    )
