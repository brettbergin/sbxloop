"""Ask, never block (intake de-gate): a filing-blocking clarifying question
@mentions its asker, persists its fallback, and — if nobody answers — files
the issue on the concierge's stated assumption instead of dropping the goal.

The marker model lives in test_chat_choices.py and the store half in
test_daemon_store.py; this file covers the bridge: registration on post,
resolution on any engagement from the asker, the sweep that turns an expired
ask into a nudge turn, and restart survival.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sbxloop.daemon.chat_choices import Choice, ChoiceQuestion, PendingFiling
from sbxloop.daemon.concierge import ConciergeReply
from tests.unit.test_daemon_discord import (
    BOT_USER,
    FakeConcierge,
    FakeMessage,
    make_bridge,
    wait_for,
)

PENDING = PendingFiling(
    question="What are you seeing that you want gone or changed?",
    assumption="grey GitHub preview cards under every bridge message",
)
ASK_REPLY = ConciergeReply("What are you seeing that you want gone or changed?", pending=PENDING)
CHOICES = ChoiceQuestion(
    prompt="Which one?",
    choices=(Choice(value="cards", label="The cards"), Choice(value="text", label="The text")),
)


def ask(bridge: Any, client: Any, text: str, *, mid: int = 601) -> FakeMessage:
    control = client.channels[42]
    msg = FakeMessage(f"<@{BOT_USER.id}> {text}", control, mid=mid, mentions=[BOT_USER])
    control.messages[mid] = msg
    bridge._handle_message(msg)
    return msg


def run_coro(bridge: Any, coro: Any) -> Any:
    import asyncio

    return asyncio.run_coroutine_threadsafe(coro, bridge._aloop).result(timeout=10)


def run_ask_turn(
    tmp_path: Path,
    name: str = "a",
    replies: list[Any] | None = None,
    wait_text: str = "What are you seeing",
) -> tuple[Any, Any, Any]:
    concierge = FakeConcierge(list(replies or [ASK_REPLY, ConciergeReply("filed it")]))
    bridge, client, _ = make_bridge(tmp_path / name, concierge=concierge)
    bridge.start()
    ask(bridge, client, "remove the embeds")
    assert wait_for(lambda: any(wait_text in s for s in client.channels[42].sent))
    return bridge, client, concierge


class TestRegistrationAndMention:
    def test_a_filing_blocking_ask_is_registered_and_persisted(self, tmp_path: Path) -> None:
        bridge, _client, _c = run_ask_turn(tmp_path)
        try:
            assert wait_for(lambda: bool(bridge.dstore.open_clarifications()))
            (row,) = bridge.dstore.open_clarifications()
            assert row.asker_id == "1"
            assert row.assumption == PENDING.assumption
            assert row.deadline > row.created_at
        finally:
            bridge.close()

    def test_the_free_text_ask_mentions_the_requester(self, tmp_path: Path) -> None:
        bridge, client, _c = run_ask_turn(tmp_path)
        try:
            posted = next(s for s in client.channels[42].sent if "What are you seeing" in s)
            assert posted.startswith("<@1> ")
        finally:
            bridge.close()

    def test_a_choice_ask_mentions_the_requester_too(self, tmp_path: Path) -> None:
        reply = ConciergeReply("Which one?", question=CHOICES, pending=PENDING)
        bridge, client, _c = run_ask_turn(
            tmp_path, replies=[reply, ConciergeReply("filed it")], wait_text="The cards"
        )
        try:
            posted = next(s for s in client.channels[42].sent if "The cards" in s)
            assert "<@1>" in posted
            assert wait_for(lambda: bool(bridge.dstore.open_clarifications()))
        finally:
            bridge.close()


class TestResolution:
    def test_a_typed_answer_resolves_the_row(self, tmp_path: Path) -> None:
        bridge, client, concierge = run_ask_turn(tmp_path)
        try:
            assert wait_for(lambda: bool(bridge.dstore.open_clarifications()))
            ask(bridge, client, "the grey preview cards", mid=602)
            assert wait_for(lambda: len(concierge.turns) == 2)
            assert wait_for(lambda: not bridge.dstore.open_clarifications())
        finally:
            bridge.close()

    def test_a_click_resolves_the_row(self, tmp_path: Path) -> None:
        reply = ConciergeReply("Which one?", question=CHOICES, pending=PENDING)
        bridge, _client, concierge = run_ask_turn(
            tmp_path, replies=[reply, ConciergeReply("filed it")], wait_text="The cards"
        )
        try:
            assert wait_for(lambda: bool(bridge._questions))
            assert wait_for(lambda: bool(bridge.dstore.open_clarifications()))
            mid = next(iter(bridge._questions))
            assert bridge._answer_choice(mid, "cards") is True
            assert wait_for(lambda: len(concierge.turns) == 2)
            assert wait_for(lambda: not bridge.dstore.open_clarifications())
        finally:
            bridge.close()

    def test_any_engagement_from_the_asker_resolves(self, tmp_path: Path) -> None:
        """The concierge handles the actual words in-session — whatever the
        asker says next stands the fallback down."""
        bridge, client, concierge = run_ask_turn(tmp_path)
        try:
            assert wait_for(lambda: bool(bridge.dstore.open_clarifications()))
            ask(bridge, client, "actually, what runs are live?", mid=603)
            assert wait_for(lambda: len(concierge.turns) == 2)
            assert wait_for(lambda: not bridge.dstore.open_clarifications())
        finally:
            bridge.close()


class TestExpiry:
    def test_expiry_announces_and_files_with_the_assumption(self, tmp_path: Path) -> None:
        bridge, client, concierge = run_ask_turn(tmp_path)
        try:
            assert wait_for(lambda: bool(bridge.dstore.open_clarifications()))
            (row,) = bridge.dstore.open_clarifications()
            run_coro(bridge, bridge._sweep_clarifications_once(now=row.deadline + 1.0))
            control = client.channels[42]
            announcement = next(s for s in control.sent if "proceeding with the assumption" in s)
            assert announcement.startswith("<@1> ")
            assert "no reply in 15m" in announcement
            assert PENDING.assumption in announcement
            assert wait_for(lambda: len(concierge.turns) == 2)
            nudge = concierge.turns[1][0]
            assert "Proceed now on your stated assumption" in nudge
            assert PENDING.assumption in nudge
            assert "Do not ask again" in nudge
            assert bridge.dstore.open_clarifications() == []
        finally:
            bridge.close()

    def test_expiry_survives_a_restart(self, tmp_path: Path) -> None:
        """The row is the durable state: a fresh bridge on the same store
        fires the fallback an old bridge armed."""
        bridge, _client, _c = run_ask_turn(tmp_path, name="same")
        assert wait_for(lambda: bool(bridge.dstore.open_clarifications()))
        (row,) = bridge.dstore.open_clarifications()
        bridge.close()

        concierge = FakeConcierge([ConciergeReply("filed it")])
        bridge2, _client2, _ = make_bridge(tmp_path / "same", concierge=concierge)
        bridge2.start()
        try:
            run_coro(bridge2, bridge2._sweep_clarifications_once(now=row.deadline + 1.0))
            assert wait_for(lambda: len(concierge.turns) == 1)
            assert "Proceed now on your stated assumption" in concierge.turns[0][0]
            assert bridge2.dstore.open_clarifications() == []
        finally:
            bridge2.close()

    def test_a_nudge_never_rearms_the_fallback(self, tmp_path: Path) -> None:
        """Even a model that answers the nudge with another ask cannot arm a
        second wait: the nudge exists to end one."""
        bridge, _client, concierge = run_ask_turn(tmp_path, replies=[ASK_REPLY, ASK_REPLY])
        try:
            assert wait_for(lambda: bool(bridge.dstore.open_clarifications()))
            (row,) = bridge.dstore.open_clarifications()
            run_coro(bridge, bridge._sweep_clarifications_once(now=row.deadline + 1.0))
            assert wait_for(lambda: len(concierge.turns) == 2)
            assert bridge.dstore.open_clarifications() == []
        finally:
            bridge.close()


class TestConfigDrivesTheWait:
    def test_clarify_ttl_config_drives_button_and_sweep_alike(self, tmp_path: Path) -> None:
        from sbxloop.config import Config
        from sbxloop.daemon.discord import DiscordBridge
        from sbxloop.daemon.store import DaemonStore
        from tests.unit.test_daemon_discord import FakeClient, FakeLoop

        config = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "discord": {"channel_id": 42},
                "concierge": {"clarify_ttl_s": 120},
            }
        )
        dstore = DaemonStore(config.paths.state_db)
        client = FakeClient(42)
        bridge = DiscordBridge(
            config,
            dstore,
            loop_ref=FakeLoop(dstore),
            client_factory=lambda b: client,
            token="tok",
        )
        assert bridge._question_ttl_s == 120.0
