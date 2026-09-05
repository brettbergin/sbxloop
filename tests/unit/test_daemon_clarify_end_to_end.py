"""#564 end to end: a clarifying question the concierge asks with enumerable
answers reaches Discord as clickable buttons, a *click alone* answers it, and
the concierge sees exactly what a typed answer would have sent.

This drives the whole chain with no network and no discord.py: a real
:class:`Concierge` (its worker replaced by the scripted FakeClient from
test_daemon_concierge) behind a real :class:`DiscordBridge` (its client
replaced by the FakeClient from test_daemon_discord), with discord.ui stubbed
so the component path runs on a host without the optional extra.

The symptom this proves gone: the bot used to post the clarifying question as
plain prose and block until a human typed a reply.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.concierge import Concierge
from sbxloop.daemon.discord import DiscordBridge
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.store import StateStore
from sbxloop.events import EventBus
from tests.unit.test_daemon_concierge import FakeClient as ScriptedWorker
from tests.unit.test_daemon_concierge import FakeHost, FakeVersions, LoopWithRuns
from tests.unit.test_daemon_discord import (
    BOT_USER,
    FakeChannel,
    FakeLoop,
    FakeMessage,
    StubButton,
    StubView,
    wait_for,
)
from tests.unit.test_daemon_discord import FakeClient as FakeDiscord

# The clarifying question at the heart of the report: the concierge needs to
# know what the user wants changed, and the plausible answers are enumerable.
CLARIFY = (
    "Before I file this — what are you seeing that you want changed?\n\n"
    "```sbx-choices\n"
    '{"prompt": "What do you want changed?", "choices": ['
    '{"value": "the wording", "label": "The wording"},'
    '{"value": "the layout", "label": "The layout"},'
    '{"value": "the timing", "label": "The timing"}]}'
    "\n```"
)
# The follow-up: same text either way, because the answer text is the same.
FILED = "Filed it — thanks."
# A question whose answers are *not* enumerable: no spec, so no components.
OPEN_ENDED = "Paste the traceback you saw and I'll take a look."


@pytest.fixture(autouse=True)
def _adapters_without_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two places the bridge touches discord.py directly, stubbed so the
    whole chain runs on a host without the optional extra."""
    from sbxloop.daemon import discord as bridge_module

    monkeypatch.setattr(bridge_module, "_to_embed", lambda spec: None)
    monkeypatch.setattr(bridge_module, "_allowed_mentions_none", lambda: "none")
    # An ask now pings its asker (ask, never block), so the mention-allowing
    # variant is reached too and needs the same stub.
    monkeypatch.setattr(bridge_module, "_allowed_mentions_users", lambda: "users")


@pytest.fixture
def stub_components(monkeypatch: pytest.MonkeyPatch) -> None:
    """discord.ui stand-ins, so the button path is exercised without the
    optional extra installed (CI syncs without it)."""
    mod = types.ModuleType("discord")
    ui = types.ModuleType("discord.ui")
    ui.View = StubView  # type: ignore[attr-defined]
    ui.Button = StubButton  # type: ignore[attr-defined]
    mod.ui = ui  # type: ignore[attr-defined]
    mod.ButtonStyle = type("ButtonStyle", (), {"secondary": "secondary"})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "discord", mod)
    monkeypatch.setitem(sys.modules, "discord.ui", ui)


class StubResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
        self.messages.append((content, ephemeral))

    async def defer(self) -> None:  # pragma: no cover - not reached here
        pass


class StubInteraction:
    """What discord.py hands a button callback when someone clicks."""

    def __init__(self, name: str = "brett") -> None:
        self.user = type("U", (), {"name": name, "display_name": name})()
        self.response = StubResponse()


def build(
    tmp_path: Path, scripts: list[dict[str, Any]]
) -> tuple[DiscordBridge, FakeDiscord, ScriptedWorker, FakeChannel]:
    """A real bridge in front of a real concierge, both on fakes."""
    cfg = Config.model_validate({"home": str(tmp_path / "state"), "discord": {"channel_id": 42}})
    dstore = DaemonStore(cfg.paths.state_db)
    worker = ScriptedWorker(scripts)
    concierge = Concierge(
        cfg,
        loop=LoopWithRuns(dstore),  # type: ignore[arg-type]
        dstore=dstore,
        store_factory=lambda: StateStore(cfg.paths.state_db),
        github=None,
        host=FakeHost(worker),
        bus=EventBus(),
        clock=lambda: 1_000_000.0,
        versions=FakeVersions(),
    )
    client = FakeDiscord(42)

    def factory(b: DiscordBridge) -> FakeDiscord:
        client.bridge = b
        return client

    bridge = DiscordBridge(
        cfg,
        dstore,
        loop_ref=FakeLoop(dstore),
        client_factory=factory,
        token="tok",
        concierge=concierge,
    )
    return bridge, client, worker, client.channels[42]


def ask(bridge: DiscordBridge, control: FakeChannel, text: str, mid: int = 900) -> FakeMessage:
    """A user @mentions the bot in the control channel."""
    msg = FakeMessage(f"<@{BOT_USER.id}> {text}", control, mentions=[BOT_USER], mid=mid)
    control.messages[mid] = msg
    bridge._handle_message(msg)
    return msg


def prompts(worker: ScriptedWorker) -> list[str]:
    """What each turn actually sent the model, minus the standing preamble."""
    return [job.prompt.split("\n---\n", 1)[-1] for job in worker.jobs]


def clarify_view(control: FakeChannel) -> Any:
    for kwargs in reversed(control.sent_kwargs):
        if "view" in kwargs:
            return kwargs["view"]
    raise AssertionError("no message carried a view")


def clarify_message(view: Any) -> Any:
    """The posted message the view is bound to. Not ``max(channel.messages)``:
    the channel holds the user's message too, and its id is the larger one."""
    message = view.handler.message
    assert message is not None, "the view was never bound to its message"
    return message


class TestClarifyingQuestionEndToEnd:
    def test_a_click_alone_answers_and_matches_the_typed_answer(
        self, tmp_path: Path, stub_components: None
    ) -> None:
        """The reported symptom, gone: the question arrives with clickable
        choices, one click advances the conversation with nobody typing, and
        the same exchange answered by typing lands identically."""
        # -- run one: answered by clicking -------------------------------------
        bridge, _client, worker, control = build(
            tmp_path / "click", [{"text": CLARIFY}, {"text": FILED}]
        )
        bridge.start()
        try:
            asked = ask(bridge, control, "something in the report looks wrong")
            assert wait_for(lambda: "✅" in asked.reactions)

            # The clarifying question is in the channel *with* buttons, one
            # per plausible answer — not prose the user must answer by hand.
            view = clarify_view(control)
            assert isinstance(view, StubView)
            assert [b.label for b in view.children] == [
                "The wording",
                "The layout",
                "The timing",
            ]
            posted_index = next(i for i, k in enumerate(control.sent_kwargs) if "view" in k)
            body = control.sent[posted_index]
            assert "what are you seeing that you want changed?" in body
            # the buttons are extra, not the only way in: prose is still there
            assert "1. The wording" in body and "your own words" in body
            # and it is threaded under the question that provoked it
            assert control.sent_kwargs[posted_index].get("reference") is asked

            # Nobody types anything: a click is the whole answer.
            before = len(worker.jobs)
            interaction = StubInteraction("brett")
            asyncio.run(view.children[1].callback(interaction))
            assert wait_for(lambda: len(worker.jobs) > before)
            assert wait_for(lambda: FILED in control.sent)

            # The click was acknowledged and the message records the answer.
            assert interaction.response.messages
            note, ephemeral = interaction.response.messages[0]
            assert ephemeral and "the layout" in note
            posted = clarify_message(view)
            assert "the layout" in posted.edit_kwargs[-1]["content"]
            assert posted.edit_kwargs[-1]["view"] is None
            clicked_prompts = prompts(worker)
        finally:
            bridge.close()

        # -- run two: the same exchange, answered by typing --------------------
        bridge2, _c2, worker2, control2 = build(
            tmp_path / "typed", [{"text": CLARIFY}, {"text": FILED}]
        )
        bridge2.start()
        try:
            asked2 = ask(bridge2, control2, "something in the report looks wrong")
            assert wait_for(lambda: "✅" in asked2.reactions)
            typed = ask(bridge2, control2, "the layout", mid=901)
            assert wait_for(lambda: "✅" in typed.reactions)
            assert wait_for(lambda: FILED in control2.sent)
            typed_prompts = prompts(worker2)
        finally:
            bridge2.close()

        # The downstream outcome is the same: the concierge saw the same
        # answer text, and answered the same way.
        assert clicked_prompts == typed_prompts
        assert clicked_prompts[-1] == "the layout"

    def test_a_typed_number_answers_the_same_question(
        self, tmp_path: Path, stub_components: None
    ) -> None:
        """A user who ignores the buttons and types "2" is understood."""
        bridge, _client, worker, control = build(tmp_path, [{"text": CLARIFY}, {"text": FILED}])
        bridge.start()
        try:
            asked = ask(bridge, control, "something looks wrong")
            assert wait_for(lambda: "✅" in asked.reactions)
            typed = ask(bridge, control, "2", mid=902)
            assert wait_for(lambda: "✅" in typed.reactions)
            assert wait_for(lambda: len(worker.jobs) == 2)
            assert prompts(worker)[-1] == "the layout"
        finally:
            bridge.close()

    def test_free_text_that_names_no_choice_passes_through_unchanged(
        self, tmp_path: Path, stub_components: None
    ) -> None:
        """Prose is still prose: answering in your own words reaches the
        concierge exactly as it was typed, as it did before #564."""
        bridge, _client, worker, control = build(tmp_path, [{"text": CLARIFY}, {"text": FILED}])
        bridge.start()
        try:
            asked = ask(bridge, control, "something looks wrong")
            assert wait_for(lambda: "✅" in asked.reactions)
            prose = "honestly it's the whole thing, top to bottom"
            typed = ask(bridge, control, prose, mid=903)
            assert wait_for(lambda: "✅" in typed.reactions)
            assert wait_for(lambda: len(worker.jobs) == 2)
            assert prompts(worker)[-1] == prose
        finally:
            bridge.close()

    def test_an_open_ended_question_is_plain_text_with_no_components(
        self, tmp_path: Path, stub_components: None
    ) -> None:
        """A question whose answers cannot be enumerated — "paste the
        traceback" — is posted as free text, with no buttons forcing an
        unsuitable set of choices on the user."""
        bridge, _client, _worker, control = build(tmp_path, [{"text": OPEN_ENDED}])
        bridge.start()
        try:
            asked = ask(bridge, control, "it blew up and I don't know why")
            assert wait_for(lambda: "✅" in asked.reactions)
            assert wait_for(lambda: OPEN_ENDED in control.sent)
            index = control.sent.index(OPEN_ENDED)
            assert "view" not in control.sent_kwargs[index]
            assert all("view" not in k for k in control.sent_kwargs)
        finally:
            bridge.close()

    def test_a_click_after_the_question_expires_still_points_at_typing(
        self, tmp_path: Path, stub_components: None
    ) -> None:
        """The interactive message degrades safely: once the question has
        expired the bridge no longer holds it, so the click is answered with
        a note rather than hanging, and typing still works."""
        bridge, _client, worker, control = build(tmp_path, [{"text": CLARIFY}, {"text": FILED}])
        bridge.start()
        try:
            asked = ask(bridge, control, "something looks wrong")
            assert wait_for(lambda: "✅" in asked.reactions)
            view = clarify_view(control)

            # Time passes: every outstanding question is past its deadline.
            with bridge._lock:
                for entry in bridge._questions.values():
                    entry.deadline = 0.0

            interaction = StubInteraction()
            asyncio.run(view.children[0].callback(interaction))
            note, ephemeral = interaction.response.messages[0]
            assert ephemeral and "type your answer" in note
            # the click did not start a turn, and nothing is blocked
            assert len(worker.jobs) == 1

            # and the question is still answerable by typing
            typed = ask(bridge, control, "the wording", mid=904)
            assert wait_for(lambda: "✅" in typed.reactions)
            assert wait_for(lambda: len(worker.jobs) == 2)
            assert prompts(worker)[-1] == "the wording"
        finally:
            bridge.close()

    def test_the_timed_out_view_says_typing_still_works(
        self, tmp_path: Path, stub_components: None
    ) -> None:
        from sbxloop.daemon.discord import TIMED_OUT_NOTE

        bridge, _client, _worker, control = build(tmp_path, [{"text": CLARIFY}])
        bridge.start()
        try:
            asked = ask(bridge, control, "something looks wrong")
            assert wait_for(lambda: "✅" in asked.reactions)
            view = clarify_view(control)
            posted = clarify_message(view)
            asyncio.run(view.on_timeout())
            assert TIMED_OUT_NOTE in posted.edit_kwargs[-1]["content"]
            assert posted.edit_kwargs[-1]["view"] is None
        finally:
            bridge.close()
