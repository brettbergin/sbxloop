"""Mattermost bridge: websocket events in, REST calls out — with a fake
client, no network, aiohttp not required. The service-agnostic behaviour
(pump, digest, status line, watches) is covered once in
test_daemon_discord.py; this file covers the Mattermost seams: event
normalisation and filtering, threads as the root post id, mention
neutralisation at the send seam, reactions by name, the permalink thread
pointer, the token check and the once-logged channel error.

``TestTyping`` and ``TestSpentAffordances`` are the parity half: the three
signals a Discord user gets from an interaction — received, still working,
affordance spent — reaching a Mattermost one through the shapes that
service actually has.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config, MattermostConfig
from sbxloop.daemon import mattermost
from sbxloop.daemon.chat import build_bridge
from sbxloop.daemon.chat_choices import Choice, ChoiceQuestion
from sbxloop.daemon.discord_format import EmbedSpec
from sbxloop.daemon.mattermost import (
    EXPIRED_CLICK_NOTE,
    MattermostApiError,
    MattermostBridge,
    MattermostMessage,
    MattermostTarget,
)
from sbxloop.daemon.mattermost_format import ZERO_WIDTH_SPACE
from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.daemon.store import ChatThread, DaemonStore
from sbxloop.errors import DaemonError
from sbxloop.events import EventBus
from tests.unit.test_daemon_discord import (
    FakeConcierge,
    FakeEngine,
    FakeLoop,
    make_gate,
    wait_for,
)

URL = "https://mm.example.com"
CHANNEL = "c" * 26
BOT_ID = "b" * 26
BOT_NAME = "sbxloop"
USER_ID = "u" * 26
TEAM_ID = "t" * 26


class FakeMattermostClient:
    """The slice of the REST + websocket client the bridge calls, recording
    everything."""

    def __init__(self, bridge: MattermostBridge) -> None:
        self.bridge = bridge
        self.posts: list[dict[str, Any]] = []
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.unreactions: list[tuple[str, str, str]] = []
        self.typing: list[tuple[str, str]] = []
        self.fail_reaction: Exception | None = None
        self.lookups: list[str] = []
        self.users = {USER_ID: "ana"}
        self.connected = False
        self.connects = 0
        self.closed = False
        self.closes = 0
        # Set when the websocket is gone, exactly as the real client's is:
        # `drop()` is a network blip, a server restart, a proxy timeout.
        self._closed: asyncio.Event | None = None
        self.fail_connect: Exception | None = None
        self.fail_post: Exception | None = None
        self.fail_upload = False
        self.uploads: list[tuple[str, str, bytes]] = []
        self.team_name = "sbx"
        self._seq = 0

    async def connect(self) -> tuple[str, str]:
        if self.fail_connect is not None:
            raise self.fail_connect
        self.connected = True
        self.connects += 1
        self._closed = asyncio.Event()
        return BOT_ID, BOT_NAME

    async def close(self) -> None:
        self.closed = True
        self.closes += 1
        if self._closed is not None:
            self._closed.set()

    async def wait_closed(self) -> None:
        if self._closed is None:
            return
        await self._closed.wait()

    def drop(self) -> None:
        """The socket goes away under the bridge, as it does on a server
        restart or an idle proxy timeout."""
        if self._closed is not None:
            self.bridge._aloop.call_soon_threadsafe(self._closed.set)  # type: ignore[union-attr]

    async def create_post(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.fail_post is not None:
            raise self.fail_post
        self._seq += 1
        post_id = f"p{self._seq:025d}"
        self.posts.append({**body, "id": post_id})
        return {"id": post_id, "channel_id": body["channel_id"]}

    async def patch_post(self, post_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.patches.append((post_id, body))
        return {"id": post_id}

    async def create_reaction(self, user_id: str, post_id: str, emoji_name: str) -> None:
        if self.fail_reaction is not None:
            raise self.fail_reaction
        self.reactions.append((user_id, post_id, emoji_name))

    async def delete_reaction(self, user_id: str, post_id: str, emoji_name: str) -> None:
        self.unreactions.append((user_id, post_id, emoji_name))

    async def send_typing(self, channel_id: str, parent_id: str = "") -> None:
        self.typing.append((channel_id, parent_id))

    async def upload_file(self, channel_id: str, name: str, content: bytes) -> str:
        if self.fail_upload:
            raise MattermostApiError(413, "too large")
        self.uploads.append((channel_id, name, content))
        return f"f{len(self.uploads):025d}"

    async def get_user(self, user_id: str) -> dict[str, Any]:
        self.lookups.append(user_id)
        if user_id not in self.users:
            raise MattermostApiError(404, "not found")
        return {"id": user_id, "username": self.users[user_id]}

    async def get_channel(self, channel_id: str) -> dict[str, Any]:
        return {"id": channel_id, "team_id": TEAM_ID}

    async def get_team(self, team_id: str) -> dict[str, Any]:
        return {"id": team_id, "name": self.team_name}

    def deliver(self, payload: dict[str, Any]) -> None:
        """What the websocket reader does with one frame."""
        self.bridge._handle_ws_event(payload)

    def react(self, post_id: str, emoji: str, *, user: str = USER_ID) -> None:
        """A reaction_added frame, as the server sends it."""
        self.deliver(
            {
                "event": "reaction_added",
                "data": {
                    "reaction": json.dumps(
                        {"user_id": user, "post_id": post_id, "emoji_name": emoji}
                    )
                },
            }
        )


def make_bridge(
    tmp_path: Path,
    *,
    concierge: Any = None,
    token: str = "mmtoken",
    **mattermost: Any,
) -> tuple[MattermostBridge, FakeMattermostClient, FakeLoop]:
    config = Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "mattermost": {"url": URL, "channel_id": CHANNEL, **mattermost},
        }
    )
    dstore = DaemonStore(config.paths.state_db)
    floop = FakeLoop(dstore)
    holder: dict[str, FakeMattermostClient] = {}

    def factory(b: MattermostBridge) -> FakeMattermostClient:
        holder["client"] = FakeMattermostClient(b)
        return holder["client"]

    bridge = MattermostBridge(
        config,
        dstore,
        loop_ref=floop,
        client_factory=factory,
        token=token,
        concierge=concierge,
    )
    bridge.start()
    return bridge, holder["client"], floop


def posted(
    text: str,
    *,
    user: str = USER_ID,
    post_id: str = "q" * 26,
    root_id: str = "",
    channel: str = CHANNEL,
    post_type: str = "",
    props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    post = {
        "id": post_id,
        "user_id": user,
        "channel_id": channel,
        "message": text,
        "root_id": root_id,
        "type": post_type,
        "props": props or {},
    }
    return {"event": "posted", "data": {"post": json.dumps(post)}, "seq": 1}


def start_run(
    bridge: MattermostBridge, run_id: str = "r1"
) -> tuple[WorkItem, EventBus, FakeEngine]:
    item = WorkItem(item_id="gh:issue:4", source_key="4", title="Fix login", url="https://x/4")
    bus = EventBus()
    engine = FakeEngine()
    bridge.run_started(item, run_id, engine, bus)  # type: ignore[arg-type]
    assert wait_for(lambda: bridge.dstore.chat_thread(run_id) is not None)
    return item, bus, engine


def thread_of(bridge: MattermostBridge, run_id: str = "r1") -> ChatThread:
    known = bridge.dstore.chat_thread(run_id)
    assert known is not None
    return known


class TestConfig:
    def test_channel_id_must_be_an_id_not_a_name(self) -> None:
        with pytest.raises(ValueError, match="26-character id"):
            MattermostConfig(url=URL, channel_id="~town-square")
        with pytest.raises(ValueError, match="26-character id"):
            MattermostConfig(url=URL, channel_id=f"{URL}/sbx/channels/town-square")
        assert MattermostConfig(url=URL, channel_id=f"  {CHANNEL} ").channel_id == CHANNEL

    def test_url_must_carry_a_scheme_and_host(self) -> None:
        with pytest.raises(ValueError, match="base URL including the scheme"):
            MattermostConfig(url="mm.example.com")
        with pytest.raises(ValueError, match="base URL including the scheme"):
            MattermostConfig(url="ftp://mm.example.com")
        # Self-hosted: a private host and a port are ordinary here.
        assert MattermostConfig(url="http://10.0.0.12:8065/").url == "http://10.0.0.12:8065"

    def test_a_channel_without_an_instance_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="channel_id is set but url is not"):
            MattermostConfig(channel_id=CHANNEL)

    def test_selected_as_the_one_configured_section(self, tmp_path: Path) -> None:
        config = Config.model_validate({"mattermost": {"url": URL, "channel_id": CHANNEL}})
        assert config.chat_backend == "mattermost"
        assert config.chat_settings is config.mattermost
        dstore = DaemonStore(config.paths.state_db)
        assert isinstance(build_bridge(config, dstore), MattermostBridge)


class TestCredentials:
    def test_missing_token_names_the_env_var(self, tmp_path: Path) -> None:
        with pytest.raises(DaemonError, match="MATTERMOST_BOT_TOKEN is not set"):
            make_bridge(tmp_path, token="")


class TestInboundFiltering:
    def test_a_system_post_is_not_routed(self, tmp_path: Path) -> None:
        bridge, client, floop = make_bridge(tmp_path)
        client.deliver(posted("!sbx pause", post_type="system_join_channel"))
        assert not wait_for(lambda: bool(floop.hold_calls), timeout=0.3)
        bridge.close()

    def test_a_post_from_another_channel_is_not_routed(self, tmp_path: Path) -> None:
        bridge, client, floop = make_bridge(tmp_path)
        client.deliver(posted("!sbx pause", channel="d" * 26))
        assert not wait_for(lambda: bool(floop.hold_calls), timeout=0.3)
        bridge.close()

    def test_our_own_post_is_not_routed(self, tmp_path: Path) -> None:
        bridge, client, floop = make_bridge(tmp_path)
        client.deliver(posted("!sbx pause", user=BOT_ID))
        assert not wait_for(lambda: bool(floop.hold_calls), timeout=0.3)
        bridge.close()

    def test_a_bot_post_is_not_routed(self, tmp_path: Path) -> None:
        bridge, client, floop = make_bridge(tmp_path)
        client.deliver(posted("!sbx pause", props={"from_bot": "true"}))
        assert not wait_for(lambda: bool(floop.hold_calls), timeout=0.3)
        bridge.close()

    def test_a_command_runs_and_the_author_handle_is_resolved(self, tmp_path: Path) -> None:
        bridge, client, floop = make_bridge(tmp_path)
        client.deliver(posted("!sbx pause"))
        assert wait_for(lambda: bool(floop.hold_calls))
        assert floop.hold_calls[0] == ("pause", "operator", "Mattermost user `ana`")
        assert client.lookups == [USER_ID]
        bridge.close()

    def test_mentioning_the_bot_reaches_the_concierge(self, tmp_path: Path) -> None:
        concierge = FakeConcierge()
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        client.deliver(posted(f"@{BOT_NAME} what is running?"))
        assert wait_for(lambda: bool(concierge.turns))
        # The mention token is stripped before the concierge sees the ask.
        assert concierge.turns[0] == ("what is running?", "Mattermost user `ana`")
        bridge.close()

    def test_an_unaddressed_post_is_ignored(self, tmp_path: Path) -> None:
        concierge = FakeConcierge()
        bridge, client, floop = make_bridge(tmp_path, concierge=concierge)
        client.deliver(posted("just talking to a colleague"))
        assert not wait_for(lambda: bool(concierge.turns) or bool(floop.hold_calls), timeout=0.3)
        bridge.close()


class TestSteering:
    def test_a_mention_in_a_run_thread_steers(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            _, _, engine = start_run(bridge)
            known = thread_of(bridge)
            client.deliver(
                posted(
                    f"@{BOT_NAME} focus on the tests first",
                    post_id="s" * 26,
                    root_id=known.thread_id,
                )
            )
            assert wait_for(lambda: engine.posted == ["focus on the tests first"])
        finally:
            bridge.close()

    def test_an_unaddressed_post_in_a_run_thread_does_not_steer(self, tmp_path: Path) -> None:
        """Being in a run's thread is not being addressed: steering pauses
        the agent and can rewrite the running task's plan, so it takes the
        same deliberate @mention the concierge does, and people can talk to
        each other in the thread."""
        bridge, client, _ = make_bridge(tmp_path)
        try:
            _, _, engine = start_run(bridge)
            known = thread_of(bridge)
            client.deliver(
                posted("agreed, that build was flaky", post_id="s" * 26, root_id=known.thread_id)
            )
            assert not wait_for(lambda: bool(engine.posted), timeout=0.3)
        finally:
            bridge.close()


class TestSendSeam:
    def test_a_post_carries_the_channel_and_no_root(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        message = asyncio.run(bridge._send(MattermostTarget(CHANNEL), "hello"))
        assert client.posts[-1]["channel_id"] == CHANNEL
        assert "root_id" not in client.posts[-1]
        assert isinstance(message, MattermostMessage)
        bridge.close()

    def test_a_thread_post_carries_the_root_id(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        root = "r" * 26
        asyncio.run(bridge._send(MattermostTarget(CHANNEL, root_id=root), "in thread"))
        assert client.posts[-1]["root_id"] == root
        bridge.close()

    def test_prose_mentions_are_made_inert(self, tmp_path: Path) -> None:
        """Mattermost has no allowed-mentions control, so agent prose
        quoting `@ana` must not ping her."""
        bridge, client, _ = make_bridge(tmp_path)
        asyncio.run(bridge._send(MattermostTarget(CHANNEL), "blamed @ana and @channel"))
        body = client.posts[-1]["message"]
        assert f"@{ZERO_WIDTH_SPACE}ana" in body and f"@{ZERO_WIDTH_SPACE}channel" in body
        assert "@ana" not in body.replace(ZERO_WIDTH_SPACE, "\x01")
        bridge.close()

    def test_an_intentional_ping_survives(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        asyncio.run(
            bridge._send(MattermostTarget(CHANNEL), "@ana your run finished", mention_users=True)
        )
        assert "@ana" in client.posts[-1]["message"]
        assert ZERO_WIDTH_SPACE not in client.posts[-1]["message"]
        bridge.close()

    def test_a_code_span_is_left_alone(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        asyncio.run(bridge._send(MattermostTarget(CHANNEL), "run `git blame @ana`"))
        assert "`git blame @ana`" in client.posts[-1]["message"]
        bridge.close()

    def test_files_ride_the_post_as_uploads(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        artifact = tmp_path / "report.md"
        artifact.write_text("x")
        asyncio.run(bridge._send(MattermostTarget(CHANNEL), "done", files=[str(artifact)]))
        assert client.uploads == [(CHANNEL, "report.md", b"x")]
        assert client.posts[-1]["file_ids"] == [f"f{1:025d}"]
        bridge.close()

    def test_a_failed_upload_is_named_never_dropped(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        client.fail_upload = True
        artifact = tmp_path / "report.md"
        artifact.write_text("x")
        asyncio.run(bridge._send(MattermostTarget(CHANNEL), "done", files=[str(artifact)]))
        assert "file_ids" not in client.posts[-1]
        assert "report.md" in client.posts[-1]["message"]
        assert str(artifact) in client.posts[-1]["message"]
        bridge.close()

    def test_a_file_over_the_cap_is_named_not_uploaded(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path, max_attachment_bytes=4)
        artifact = tmp_path / "big.bin"
        artifact.write_bytes(b"0123456789")
        asyncio.run(bridge._send(MattermostTarget(CHANNEL), "done", files=[str(artifact)]))
        assert client.uploads == []
        assert "too large to attach" in client.posts[-1]["message"]
        bridge.close()


class TestEditAndReact:
    def test_an_edit_patches_the_post(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        message = MattermostMessage(CHANNEL, "p" * 26)
        asyncio.run(bridge._edit(message, "updated"))
        assert client.patches[-1] == ("p" * 26, {"message": "updated"})
        bridge.close()

    def test_a_reaction_goes_by_emoji_name(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        asyncio.run(bridge._add_reaction(MattermostMessage(CHANNEL, "p" * 26), "✅"))
        assert client.reactions[-1] == (BOT_ID, "p" * 26, "white_check_mark")
        bridge.close()

    def test_an_unmapped_emoji_is_a_programming_error(self, tmp_path: Path) -> None:
        bridge, _, _ = make_bridge(tmp_path)
        with pytest.raises(ValueError, match="no Mattermost reaction name"):
            asyncio.run(bridge._add_reaction(MattermostMessage(CHANNEL, "p" * 26), "🦆"))
        bridge.close()


class TestThreads:
    def test_a_thread_is_the_reply_stream_under_the_headline(self, tmp_path: Path) -> None:
        bridge, _, _ = make_bridge(tmp_path)
        headline = MattermostMessage(CHANNEL, "h" * 26)
        target = asyncio.run(bridge._create_thread(headline, "Fix login"))
        assert target == MattermostTarget(CHANNEL, root_id="h" * 26)
        # The persisted thread id is the headline's post id.
        assert bridge._handle_id(target) == "h" * 26
        bridge.close()

    def test_thread_link_is_a_permalink(self, tmp_path: Path) -> None:
        bridge, _, _ = make_bridge(tmp_path)
        thread = ChatThread(CHANNEL, "h" * 26, None, None, "mattermost")
        assert bridge.thread_link(thread) == f"[thread]({URL}/sbx/pl/{'h' * 26})"
        bridge.close()

    def test_thread_link_without_a_team_is_the_bare_id(self, tmp_path: Path) -> None:
        bridge, _, _ = make_bridge(tmp_path)
        bridge._team = ""
        thread = ChatThread(CHANNEL, "h" * 26, None, None, "mattermost")
        assert bridge.thread_link(thread) == "h" * 26
        bridge.close()


class TestMentions:
    def test_a_stored_user_id_renders_as_a_handle(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        client.deliver(posted("!sbx status"))
        assert wait_for(lambda: bool(client.lookups))
        assert bridge.mention_user(USER_ID) == "@ana"
        bridge.close()

    def test_an_unknown_id_renders_as_itself_not_a_broken_ping(self, tmp_path: Path) -> None:
        bridge, _, _ = make_bridge(tmp_path)
        assert bridge.mention_user("z" * 26) == "@" + "z" * 26
        bridge.close()

    def test_only_this_service_s_ids_are_owned(self, tmp_path: Path) -> None:
        bridge, _, _ = make_bridge(tmp_path)
        assert bridge._owns_user_id(USER_ID)
        assert not bridge._owns_user_id("U0123ABCDEF")  # Slack
        assert not bridge._owns_user_id("123456789012345678")  # Discord
        bridge.close()


class TestChannelErrors:
    def test_an_unreachable_channel_is_reported_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        client.fail_post = MattermostApiError(403, "permission denied")
        with caplog.at_level(logging.ERROR):
            for _ in range(3):
                assert asyncio.run(bridge._send(MattermostTarget(CHANNEL), "x")) is None
        unreachable = [r for r in caplog.records if "channel_unreachable" in r.getMessage()]
        assert len(unreachable) == 1
        bridge.close()


class TestCards:
    def test_a_card_becomes_a_coloured_attachment(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        spec = EmbedSpec(title="Fix login", description="run r1", color=0x00FF00)
        asyncio.run(bridge._send(MattermostTarget(CHANNEL), "", embed=spec))
        (attachment,) = client.posts[-1]["props"]["attachments"]
        assert attachment["title"] == "Fix login"
        assert attachment["color"] == "#00FF00"
        assert attachment["text"] == "run r1"
        bridge.close()

    def test_a_rejected_card_retries_text_only(self, tmp_path: Path) -> None:
        """A run's chronology never goes missing over presentation."""
        bridge, client, _ = make_bridge(tmp_path)
        calls: list[dict[str, Any]] = []
        original = client.create_post

        async def once(body: dict[str, Any]) -> dict[str, Any]:
            calls.append(body)
            if "props" in body:
                raise MattermostApiError(400, "invalid props")
            return await original(body)

        client.create_post = once  # type: ignore[method-assign]
        asyncio.run(
            bridge._send(MattermostTarget(CHANNEL), "done", embed=EmbedSpec(title="t", color=1))
        )
        assert len(calls) == 2 and "props" not in calls[1]
        assert client.posts[-1]["message"] == "done"
        bridge.close()

    def test_embeds_off_renders_the_card_as_text(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path, embeds=False)
        asyncio.run(bridge._send(MattermostTarget(CHANNEL), "", embed=EmbedSpec(title="Fix login")))
        assert "props" not in client.posts[-1]
        assert "Fix login" in client.posts[-1]["message"]
        bridge.close()

    def test_an_edit_carries_the_card(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        asyncio.run(
            bridge._edit(
                MattermostMessage(CHANNEL, "p" * 26), "updated", embed=EmbedSpec(title="t", color=2)
            )
        )
        post_id, body = client.patches[-1]
        assert post_id == "p" * 26 and body["message"] == "updated"
        assert body["props"]["attachments"][0]["color"] == "#000002"
        bridge.close()

    def test_a_card_cannot_ping_anyone(self, tmp_path: Path) -> None:
        """An attachment's text pings exactly as a post's does."""
        bridge, client, _ = make_bridge(tmp_path)
        asyncio.run(
            bridge._send(MattermostTarget(CHANNEL), "", embed=EmbedSpec(description="by @ana"))
        )
        text = client.posts[-1]["props"]["attachments"][0]["text"]
        assert f"@{ZERO_WIDTH_SPACE}ana" in text
        bridge.close()


class TestReactionChoices:
    def test_a_question_is_seeded_with_one_emoji_per_choice(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        question = ChoiceQuestion(
            prompt="Which base?",
            choices=[Choice(value="main", label="main"), Choice(value="dev", label="dev")],
        )
        posted_msg = asyncio.run(bridge._send_choices(MattermostTarget(CHANNEL), "", question))
        assert posted_msg is not None
        seeded = [r for r in client.reactions if r[1] == posted_msg.post_id]
        assert [r[2] for r in seeded] == ["one", "two"]
        # the numbered prose is still the body, so typing answers too
        assert "main" in client.posts[-1]["message"]
        bridge.close()

    def test_reacting_answers_the_question(self, tmp_path: Path) -> None:
        concierge = FakeConcierge()
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        question = ChoiceQuestion(
            prompt="Which base?",
            choices=[Choice(value="main", label="main"), Choice(value="dev", label="dev")],
        )
        asker = bridge._inbound(
            json.loads(posted("@sbxloop which base?")["data"]["post"])  # type: ignore[arg-type]
        )
        assert asker is not None
        posted_msg = asyncio.run(bridge._send_choices(MattermostTarget(CHANNEL), "", question))
        assert posted_msg is not None
        bridge._register_question(posted_msg.post_id, question, asker)
        client.react(posted_msg.post_id, "two")
        assert wait_for(lambda: bool(concierge.turns))
        assert "dev" in concierge.turns[0][0]
        # the post says what was chosen and by whom
        assert wait_for(lambda: any("Answered" in b["message"] for _, b in client.patches))
        bridge.close()

    def test_our_own_seeded_reaction_is_not_an_answer(self, tmp_path: Path) -> None:
        concierge = FakeConcierge()
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        client.react("p" * 26, "one", user=BOT_ID)
        assert not wait_for(lambda: bool(concierge.turns), timeout=0.3)
        bridge.close()

    def test_a_reaction_on_an_unknown_question_is_ignored(self, tmp_path: Path) -> None:
        concierge = FakeConcierge()
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        client.react("p" * 26, "one")
        assert not wait_for(lambda: bool(concierge.turns), timeout=0.3)
        bridge.close()


class TestReactionGate:
    def test_the_gate_prompt_is_seeded_and_reacting_approves(self, tmp_path: Path) -> None:
        bridge, client, floop = make_bridge(tmp_path)
        approved: list[tuple[str, str]] = []

        def approve_merge(run_id: str, *, by: str) -> str:
            approved.append((run_id, by))
            return "merging"

        floop.approve_merge = approve_merge  # type: ignore[attr-defined]
        gate = make_gate("r1")
        posted_msg = asyncio.run(bridge._send_gate(MattermostTarget(CHANNEL), "approve?", gate))
        assert posted_msg is not None
        assert (BOT_ID, posted_msg.post_id, "white_check_mark") in client.reactions
        client.react(posted_msg.post_id, "white_check_mark")
        assert wait_for(lambda: bool(approved))
        assert approved[0] == ("r1", "Mattermost user `ana`")
        bridge.close()

    def test_a_gate_prompt_from_before_a_restart_is_found_in_the_store(
        self, tmp_path: Path
    ) -> None:
        """The post id -> run map is in memory; the prompt is not."""
        bridge, client, floop = make_bridge(tmp_path)
        approved: list[tuple[str, str]] = []

        def approve_merge(run_id: str, *, by: str) -> str:
            approved.append((run_id, by))
            return "merging"

        floop.approve_merge = approve_merge  # type: ignore[attr-defined]
        bridge.dstore.create_merge_gate(
            "r1",
            "gh:issue:4",
            "you/repo",
            7,
            "https://x/pull/7",
            "b",
            [],
            "tok1",
            time.time(),
        )
        prompt_id = "g" * 26
        bridge.dstore.set_gate_prompt("r1", CHANNEL, prompt_id, backend="mattermost")
        bridge._gate_posts.clear()
        client.react(prompt_id, "white_check_mark")
        assert wait_for(lambda: bool(approved))
        assert approved[0][0] == "r1"
        bridge.close()


class TestTyping:
    """Discord's typing indicator, in the shape Mattermost has for it.

    The ⏳ ack says a message was *received*; a concierge turn that thinks
    for a minute needs something that says the daemon is *still on it*, or
    the human is left wondering whether their ask was picked up at all.
    """

    def test_a_concierge_turn_says_the_bot_is_typing(self, tmp_path: Path) -> None:
        concierge = FakeConcierge()
        concierge.gate.clear()  # hold the turn open so the beat has to run
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        try:
            client.deliver(posted(f"@{BOT_NAME} what is running?"))
            assert wait_for(lambda: bool(client.typing))
            assert client.typing[0] == (CHANNEL, "")
        finally:
            concierge.gate.set()
            bridge.close()

    def test_the_indicator_stops_when_the_turn_answers(self, tmp_path: Path) -> None:
        concierge = FakeConcierge()
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        try:
            client.deliver(posted(f"@{BOT_NAME} what is running?"))
            assert wait_for(lambda: bool(client.posts))
            settled = len(client.typing)
            assert not wait_for(lambda: len(client.typing) > settled, timeout=0.5)
        finally:
            bridge.close()

    def test_a_typing_frame_the_service_refuses_does_not_fail_the_turn(
        self, tmp_path: Path
    ) -> None:
        concierge = FakeConcierge()
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)

        async def refuse(channel_id: str, parent_id: str = "") -> None:
            raise MattermostApiError(500, "websocket gone")

        client.send_typing = refuse  # type: ignore[assignment]
        try:
            client.deliver(posted(f"@{BOT_NAME} what is running?"))
            assert wait_for(lambda: any("hello" in p["message"] for p in client.posts))
        finally:
            bridge.close()

    def test_the_frame_carries_the_thread_when_the_ask_is_in_one(self, tmp_path: Path) -> None:
        """A ``user_typing`` frame is scoped to a channel *and* a root post,
        so the indicator shows where the person is actually looking."""
        bridge, client, _ = make_bridge(tmp_path)
        try:
            asyncio.run(bridge._send_typing(MattermostTarget(CHANNEL, root_id="r" * 26)))
            assert client.typing == [(CHANNEL, "r" * 26)]
        finally:
            bridge.close()


class TestRefusedReactions:
    def test_a_refused_reaction_is_reported_once_with_the_emoji_name(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An instance whose emoji set does not carry the name the bridge
        reacts with costs a human the ack that says their ask was picked up
        — and used to do it with nothing in the log. One warning per name
        names the emoji and quotes what the server said."""
        bridge, client, _ = make_bridge(tmp_path)
        client.fail_reaction = MattermostApiError(400, "Invalid or missing emoji_name parameter")
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                asyncio.run(bridge._add_reaction(MattermostMessage(CHANNEL, "p" * 26), "⏳"))
        refused = [r for r in caplog.records if "reaction_refused" in r.getMessage()]
        assert len(refused) == 1
        fields = refused[0].msg
        assert isinstance(fields, dict)
        assert fields["emoji_name"] == "hourglass_flowing_sand"
        assert fields["detail"] == "Invalid or missing emoji_name parameter"
        bridge.close()

    def test_a_refusal_that_is_not_a_400_still_raises(self, tmp_path: Path) -> None:
        """A 403 is the bot missing a permission, not an emoji this server
        does not know: the caller logs it with its own context."""
        bridge, client, _ = make_bridge(tmp_path)
        client.fail_reaction = MattermostApiError(403, "permission denied")
        with pytest.raises(MattermostApiError):
            asyncio.run(bridge._add_reaction(MattermostMessage(CHANNEL, "p" * 26), "⏳"))
        bridge.close()

    def test_each_emoji_is_reported_on_its_own(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        client.fail_reaction = MattermostApiError(400, "nope")
        with caplog.at_level(logging.WARNING):
            for emoji in ("⏳", "✅", "⏳"):
                asyncio.run(bridge._add_reaction(MattermostMessage(CHANNEL, "p" * 26), emoji))
        refused = [r for r in caplog.records if "reaction_refused" in r.getMessage()]
        assert len(refused) == 2
        bridge.close()


class TestSpentAffordances:
    """A seeded emoji is this bridge's button; one left on a settled prompt
    is a button that can only fail. Discord clears its view and Slack its
    blocks — these are the same act, in reactions."""

    def test_an_answered_question_keeps_its_body_and_loses_its_digits(self, tmp_path: Path) -> None:
        concierge = FakeConcierge()
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        question = ChoiceQuestion(
            prompt="Which base?",
            choices=[Choice(value="main", label="main"), Choice(value="dev", label="dev")],
        )
        asker = bridge._inbound(
            json.loads(posted("@sbxloop which base?")["data"]["post"])  # type: ignore[arg-type]
        )
        assert asker is not None
        posted_msg = asyncio.run(bridge._send_choices(MattermostTarget(CHANNEL), "", question))
        assert posted_msg is not None
        bridge._register_question(posted_msg.post_id, question, asker)
        client.react(posted_msg.post_id, "two")
        assert wait_for(lambda: bool(client.patches))
        rewritten = client.patches[-1][1]["message"]
        # the question is still readable above the answer, as on Discord
        assert "Which base?" in rewritten
        assert "_Answered: **dev** (by ana)._" in rewritten
        assert wait_for(lambda: len(client.unreactions) == 2)
        assert [r[2] for r in client.unreactions] == ["one", "two"]
        bridge.close()

    def test_a_resolved_gate_loses_its_approve_reaction(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        gate = make_gate("r1")
        posted_msg = asyncio.run(bridge._send_gate(MattermostTarget(CHANNEL), "approve?", gate))
        assert posted_msg is not None
        asyncio.run(bridge._finalize_gate_message(posted_msg, "✅ approved by ana — merged"))
        assert client.patches[-1][1]["message"] == "✅ approved by ana — merged"
        assert client.unreactions == [(BOT_ID, posted_msg.post_id, "white_check_mark")]
        # and the prompt no longer answers a reaction as a live gate
        assert posted_msg.post_id not in bridge._gate_posts
        bridge.close()

    def test_a_stale_reaction_is_told_to_type_the_answer_once(self, tmp_path: Path) -> None:
        """Discord and Slack answer a late click privately; a Mattermost bot
        account has no ephemeral post, so the nudge goes in the thread under
        the question — once, however many people try it."""
        bridge, client, _ = make_bridge(tmp_path)
        question = ChoiceQuestion(
            prompt="Which base?",
            choices=[Choice(value="main", label="main"), Choice(value="dev", label="dev")],
        )
        posted_msg = asyncio.run(bridge._send_choices(MattermostTarget(CHANNEL), "", question))
        assert posted_msg is not None
        # never registered: the same shape as a question the TTL dropped
        client.react(posted_msg.post_id, "one")
        assert wait_for(lambda: any(EXPIRED_CLICK_NOTE in p["message"] for p in client.posts))
        nudge = next(p for p in client.posts if EXPIRED_CLICK_NOTE in p["message"])
        assert nudge["root_id"] == posted_msg.post_id
        client.react(posted_msg.post_id, "two", user="d" * 26)
        assert not wait_for(
            lambda: len([p for p in client.posts if EXPIRED_CLICK_NOTE in p["message"]]) > 1,
            timeout=0.3,
        )
        bridge.close()

    def test_a_reaction_on_a_post_we_never_seeded_says_nothing(self, tmp_path: Path) -> None:
        """A digit on somebody's ordinary post is somebody's ordinary emoji,
        not a misfired click."""
        bridge, client, _ = make_bridge(tmp_path)
        client.react("p" * 26, "one")
        assert not wait_for(lambda: bool(client.posts), timeout=0.3)
        bridge.close()


class TestPostAttachmentCap:
    def test_files_beyond_the_per_post_cap_are_named_not_dropped(self, tmp_path: Path) -> None:
        """Mattermost takes five files on a post (Discord takes ten). The
        sixth is named with its host path, the way an oversized one is."""
        bridge, client, _ = make_bridge(tmp_path)
        paths = []
        for index in range(7):
            artifact = tmp_path / f"part{index}.txt"
            artifact.write_text("x")
            paths.append(str(artifact))
        asyncio.run(bridge._send(MattermostTarget(CHANNEL), "done", files=paths))
        assert len(client.uploads) == 5
        assert len(client.posts[-1]["file_ids"]) == 5
        body = client.posts[-1]["message"]
        assert "part5.txt" in body and "part6.txt" in body
        assert "part0.txt" not in body
        bridge.close()


class TestReconnect:
    """The websocket is the only way anything reaches the daemon from chat.
    REST keeps working when it drops, so a bridge that does not rebuild it
    goes on posting a run's chronology while every steer, command and
    @mention is silently discarded."""

    def test_a_dropped_connection_is_rebuilt(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            assert wait_for(lambda: client.connects == 1)
            client.drop()
            assert wait_for(lambda: client.connects == 2)
            # the dead session is let go before the next dial, or the client
            # leaks one HTTP session per outage
            assert client.closes >= 1
        finally:
            bridge.close()

    def test_the_bridge_hears_again_after_a_reconnect(self, tmp_path: Path) -> None:
        """The point of the reconnect: an @mention after the outage is
        answered, where before it vanished."""
        concierge = FakeConcierge()
        bridge, client, _ = make_bridge(tmp_path, concierge=concierge)
        try:
            assert wait_for(lambda: client.connects == 1)
            client.drop()
            assert wait_for(lambda: client.connects == 2)
            client.deliver(posted(f"@{BOT_NAME} what is running?"))
            assert wait_for(lambda: bool(concierge.turns))
        finally:
            bridge.close()

    def test_a_reconnect_that_fails_backs_off_and_keeps_trying(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An instance that comes back later must find the daemon still
        listening: the loop never gives up."""
        monkeypatch.setattr(mattermost, "RECONNECT_MIN_S", 0.01)
        monkeypatch.setattr(mattermost, "RECONNECT_MAX_S", 0.02)
        bridge, client, _ = make_bridge(tmp_path)
        try:
            assert wait_for(lambda: client.connects == 1)
            client.fail_connect = MattermostApiError(502, "bad gateway")
            client.drop()
            assert wait_for(lambda: client.closes >= 3)  # retried, not given up on
            assert client.connects == 1
            client.fail_connect = None
            assert wait_for(lambda: client.connects == 2)
        finally:
            bridge.close()

    def test_the_first_connect_is_not_retried(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A bad token or a wrong URL is a configuration error: the base
        bridge reports it once and carries on degraded. Retrying it forever
        would bury the one message that says what to fix."""
        config = Config.model_validate(
            {"home": str(tmp_path / "state"), "mattermost": {"url": URL, "channel_id": CHANNEL}}
        )
        dstore = DaemonStore(config.paths.state_db)
        holder: dict[str, FakeMattermostClient] = {}

        def factory(b: MattermostBridge) -> FakeMattermostClient:
            holder["client"] = FakeMattermostClient(b)
            holder["client"].fail_connect = MattermostApiError(401, "invalid or expired session")
            return holder["client"]

        bridge = MattermostBridge(
            config, dstore, loop_ref=FakeLoop(dstore), client_factory=factory, token="mmtoken"
        )
        with caplog.at_level(logging.ERROR):
            bridge.start()
            assert wait_for(lambda: any("connect_failed" in r.getMessage() for r in caplog.records))
        assert holder["client"].connects == 0
        bridge.close()

    def test_closing_during_an_outage_does_not_dial_again(self, tmp_path: Path) -> None:
        """A shutdown never sits through a backoff, and never races a dial
        against the close that is tearing the client down."""
        bridge, client, _ = make_bridge(tmp_path)
        client.fail_connect = MattermostApiError(502, "bad gateway")
        assert wait_for(lambda: client.connects == 1)
        client.drop()
        bridge.close()
        settled = client.connects
        assert not wait_for(lambda: client.connects > settled, timeout=0.3)


class TestMentionHandles:
    """A Mattermost mention is `@username`, so an id alone cannot be one.
    Every notice built from *stored* ids — a run watch, a gate's notify
    list — is sent exactly when the name cache the inbound path fills is
    empty, which is what made these pings text instead of notifications."""

    def test_a_watcher_from_before_a_restart_is_pinged_by_handle(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            item, _, _ = start_run(bridge)
            # The watch is in the store; the name cache is not — the shape a
            # restart leaves behind, and the shape that used to post a bare
            # 26-character id at the person who asked to be told.
            bridge.dstore.add_run_watch("r1", USER_ID, time.time(), backend="mattermost")
            bridge._watchers["r1"] = [USER_ID]
            assert USER_ID not in bridge._names
            bridge.run_finished(item, RunReport("r1", "completed", "done"))
            marker = "run `r1` finished"
            assert wait_for(lambda: any(marker in p["message"] for p in client.posts))
            notice = next(p for p in client.posts if marker in p["message"])
            assert notice["message"].startswith("@ana ")
            assert USER_ID not in notice["message"]
        finally:
            bridge.close()

    def test_a_gate_prompt_pings_its_notify_list_by_handle(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            start_run(bridge)
            asyncio.run(bridge._post_gate_prompt(make_gate("r1", notify=(USER_ID,))))
            prompt = client.posts[-1]["message"]
            assert prompt.startswith("@ana ")
        finally:
            bridge.close()

    def test_a_handle_is_looked_up_once_per_id(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        try:
            asyncio.run(bridge._resolve_mentions([USER_ID, USER_ID]))
            asyncio.run(bridge._resolve_mentions([USER_ID]))
            assert client.lookups == [USER_ID]
        finally:
            bridge.close()

    def test_an_id_that_cannot_be_resolved_is_not_retried_on_every_notice(
        self, tmp_path: Path
    ) -> None:
        """A deactivated account 404s. It renders as itself — a broken ping
        is better than a wrong one — and is not looked up again."""
        bridge, client, _ = make_bridge(tmp_path)
        gone = "z" * 26
        try:
            asyncio.run(bridge._resolve_mentions([gone]))
            asyncio.run(bridge._resolve_mentions([gone]))
            assert client.lookups == [gone]
            assert bridge.mention_user(gone) == f"@{gone}"
        finally:
            bridge.close()

    def test_another_service_s_ids_are_left_alone(self, tmp_path: Path) -> None:
        """Several bridges can run at once and watcher ids are backend-less
        in the store: a Discord snowflake must not become a users lookup."""
        bridge, client, _ = make_bridge(tmp_path)
        try:
            asyncio.run(bridge._resolve_mentions(["1234567890"]))
            assert client.lookups == []
        finally:
            bridge.close()


class TestLinkPreviews:
    """Mattermost embeds under a post are chosen from the *first autolink*
    in its message, so a chronology of PR, issue and CI links grows a
    website card under every one. A markdown link is never a candidate."""

    def test_a_bare_url_becomes_a_link_to_itself(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        asyncio.run(bridge._send(MattermostTarget(CHANNEL), "PR https://git/pull/7 is green"))
        assert client.posts[-1]["message"] == (
            "PR [https://git/pull/7](https://git/pull/7) is green"
        )
        bridge.close()

    def test_an_angle_bracketed_url_is_defused_too(self, tmp_path: Path) -> None:
        """CommonMark makes `<url>` an autolink, so Discord's suppression
        trick would still unfurl here — it is the wrong one to copy."""
        bridge, client, _ = make_bridge(tmp_path)
        asyncio.run(bridge._send(MattermostTarget(CHANNEL), "🔀 PR #7 <https://git/pull/7>"))
        assert client.posts[-1]["message"] == "🔀 PR #7 [https://git/pull/7](https://git/pull/7)"
        bridge.close()

    def test_a_markdown_link_is_left_exactly_as_it_is(self, tmp_path: Path) -> None:
        """A link inside a link corrupts both."""
        bridge, client, _ = make_bridge(tmp_path)
        asyncio.run(bridge._send(MattermostTarget(CHANNEL), "see [PR #7](https://git/pull/7)"))
        assert client.posts[-1]["message"] == "see [PR #7](https://git/pull/7)"
        bridge.close()

    def test_sentence_punctuation_stays_outside_the_link(self, tmp_path: Path) -> None:
        bridge, client, _ = make_bridge(tmp_path)
        asyncio.run(bridge._send(MattermostTarget(CHANNEL), "merged at https://git/c/abc."))
        assert client.posts[-1]["message"] == "merged at [https://git/c/abc](https://git/c/abc)."
        bridge.close()

    def test_a_url_in_a_code_span_is_left_alone(self, tmp_path: Path) -> None:
        """Nothing in a code span is a link to Mattermost either, and
        rewriting one would corrupt what the agent is quoting."""
        bridge, client, _ = make_bridge(tmp_path)
        asyncio.run(bridge._send(MattermostTarget(CHANNEL), "run `curl https://git/pull/7`"))
        assert client.posts[-1]["message"] == "run `curl https://git/pull/7`"
        bridge.close()

    def test_an_edit_defuses_the_same_way(self, tmp_path: Path) -> None:
        """A status line edited in place must not grow a card on the edit."""
        bridge, client, _ = make_bridge(tmp_path)
        asyncio.run(bridge._edit(MattermostMessage(CHANNEL, "p" * 26), "done https://git/pull/7"))
        assert client.patches[-1][1]["message"] == ("done [https://git/pull/7](https://git/pull/7)")
        bridge.close()

    def test_several_links_in_one_post_are_all_defused(self, tmp_path: Path) -> None:
        """Only the first autolink is embedded, but which one is first is
        not this seam's business to reason about."""
        bridge, client, _ = make_bridge(tmp_path)
        asyncio.run(bridge._send(MattermostTarget(CHANNEL), "https://a/1 then https://b/2"))
        assert client.posts[-1]["message"] == (
            "[https://a/1](https://a/1) then [https://b/2](https://b/2)"
        )
        bridge.close()

    def test_an_intentional_ping_still_gets_its_links_defused(self, tmp_path: Path) -> None:
        """Mention safety and preview safety are separate axes: a watch
        notice pings people *and* carries the PR link."""
        bridge, client, _ = make_bridge(tmp_path)
        asyncio.run(
            bridge._send(MattermostTarget(CHANNEL), "@ana https://git/pull/7", mention_users=True)
        )
        assert client.posts[-1]["message"] == "@ana [https://git/pull/7](https://git/pull/7)"
        bridge.close()
