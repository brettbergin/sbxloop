"""The console's chat: the address gesture, what gets written, buttons,
edits and reactions repainting in place, and read-only mode — driven
against the real local bridge's mailbox rows."""

from __future__ import annotations

import json
import time

from textual.widgets import Button

from sbxloop.daemon.mailbox import MailboxClient
from sbxloop.daemon.store import DaemonStore
from sbxloop.paths import SbxloopHome
from sbxloop.tui.chat import ChannelTail, choice_spec, compose_outbound, is_addressed
from sbxloop.tui.screens.chat import ChatScreen
from sbxloop.tui.widgets.chat_input import ChatInput
from sbxloop.tui.widgets.message import ChoiceButton
from sbxloop.tui.widgets.thread import ThreadView
from tests.unit.tui.conftest import drive, make_app


def test_compose_outbound_mirrors_the_routing_rules() -> None:
    assert compose_outbound("!sbx status", addressed=True) == "!sbx status"
    assert compose_outbound("what's running?", addressed=True) == "@sbx what's running?"
    assert compose_outbound("hey @sbx what's up", addressed=True) == "hey @sbx what's up"
    assert compose_outbound("talking to a colleague", addressed=False) == "talking to a colleague"
    assert compose_outbound("   ", addressed=True) == ""
    assert is_addressed("@sbx hi") and is_addressed("!sbx queue") and not is_addressed("hi")
    button = ChoiceButton(12, 2, "timing", "Timing")
    assert button.row_id == 12 and button.value == "timing" and button.id == "choice-12-2"


def seed_chat(state_dir: SbxloopHome) -> DaemonStore:
    dstore = DaemonStore(state_dir.state_db)
    now = time.time()
    dstore.local_post("control", "🚀 daemon started", now=now - 60)
    dstore.local_post(
        "control",
        "Which one?\n\n1. Layout\n2. Timing",
        now=now - 30,
        kind="choices",
        choices_json=json.dumps(
            {
                "prompt": "Which one?",
                "choices": [
                    {"value": "layout", "label": "Layout"},
                    {"value": "timing", "label": "Timing"},
                ],
                "expires_at": now + 600,
                "answered": None,
            }
        ),
    )
    return dstore


def test_chat_screen_shows_rows_sends_addressed_text_and_clicks(seeded: SbxloopHome) -> None:
    dstore = seed_chat(seeded)

    async def scenario() -> None:
        app = make_app(seeded, refresh_s=3.0)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.5)
            await pilot.press("4")
            await pilot.pause(1.0)
            assert isinstance(app.screen, ChatScreen)
            view = app.screen.query_one(ThreadView)
            assert len(view.widgets) == 2
            assert len(app.screen.query(Button)) == 2, "one button per choice"
            box = app.screen.query_one(ChatInput)
            box.focus()
            # Typed text is set on the form rather than pressed key by key:
            # a CI runner spends the pilot's screen wait on the pollers.
            box.value = "talking to myself"
            await pilot.press("enter")
            await pilot.pause(0.5)
            rows = dstore.local_messages("control")
            assert rows[-1].direction == "in" and rows[-1].text == "talking to myself"
            await pilot.press("ctrl+t")
            box.value = "what is running"
            await pilot.press("enter")
            await pilot.pause(0.5)
            rows = dstore.local_messages("control")
            assert rows[-1].text == "@sbx what is running" and rows[-1].author_id == "brett"
            assert box.addressed, "the gesture is sticky"
            box.value = "!sbx status"
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert dstore.local_messages("control")[-1].text == "!sbx status"
            # The console's own rows show, dimmed until the daemon takes them.
            own = [w for w in view.widgets.values() if w.row.direction == "in"]
            assert own and all(w.pending for w in own)
            dstore.take_local_inbound(now=time.time())
            app.action_refresh()  # what the next refresh tick does
            await pilot.pause(1.0)
            assert not any(w.pending for w in own)
            # A choice click writes a `choice` row replying to the question.
            question = next(r for r in dstore.local_messages("control") if r.kind == "choices")
            await pilot.click(f"#choice-{question.id}-2")
            await pilot.pause(0.5)
            click = dstore.local_messages("control")[-1]
            assert (
                click.kind == "choice"
                and click.text == "timing"
                and click.reply_to_id == question.id
            )
            # Esc leaves the form; `r` then replies to the bot's newest row.
            box.focus()
            await pilot.press("ctrl+t")  # the sticky gesture off again
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert not box.has_focus
            await pilot.press("r")
            await pilot.pause(0.2)
            assert box.reply_to is not None and box.has_focus
            box.value = "and why?"
            await pilot.press("enter")
            await pilot.pause(0.5)
            reply = dstore.local_messages("control")[-1]
            assert reply.reply_to_id is not None
            assert reply.text == "and why?", "a reply is addressed by the bridge's own rule"

    drive(scenario)


def test_edits_reactions_and_gate_resolution_repaint_in_place(seeded: SbxloopHome) -> None:
    dstore = seed_chat(seeded)
    dstore.create_merge_gate("r_live", "gh:issue:41", "o/r", 172, "u", None, ["brett"], "tok", 1.0)
    prompt = dstore.local_post(
        "control", "⏸ ready to merge", now=time.time(), kind="gate", gate_run_id="r_live"
    )

    async def scenario() -> None:
        app = make_app(seeded, refresh_s=3.0)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.5)
            await pilot.press("4")
            await pilot.pause(1.5)
            view = app.screen.query_one(ThreadView)
            first = next(iter(view.widgets.values()))
            widget_ids_before = {id(w) for w in view.widgets.values()}
            assert app.screen.query_one(f"#approve-{prompt}", Button).disabled is False
            dstore.local_edit(first.row.id, "🚀 daemon started (edited)", now=time.time())
            dstore.local_react(first.row.id, "✅", now=time.time())
            app.action_refresh()
            await pilot.pause(1.0)
            assert first.row.text.endswith("(edited)") and "✅" in first.row.reactions
            assert {id(w) for w in view.widgets.values()} == widget_ids_before, "never re-mounted"
            await pilot.click(f"#approve-{prompt}")
            await pilot.pause(0.5)
            assert dstore.local_messages("control")[-1].kind == "approve"
            dstore.resolve_merge_gate("r_live", "merged", "brett", time.time())
            dstore.local_clear_gate(prompt, now=time.time())
            app.action_refresh()
            await pilot.pause(1.0)
            assert app.screen.query_one(f"#approve-{prompt}", Button).disabled is True

    drive(scenario)


def test_read_only_console_sends_nothing(seeded: SbxloopHome) -> None:
    seed_chat(seeded)

    async def scenario() -> None:
        from sbxloop.config import Config
        from sbxloop.tui.app import SbxloopTui
        from tests.unit.tui.conftest import FakeCtl, live_status

        config = Config.model_validate({"home": str(seeded)})
        mailbox = MailboxClient(seeded.state_db, operator_id="brett")
        app = SbxloopTui(
            config, seeded, mailbox=mailbox, ctl=FakeCtl(live_status()), read_only=True
        )
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.5)
            await pilot.press("4")
            await pilot.pause(1.0)
            assert app.screen.query_one(ChatInput).disabled
            assert app.chat.send("control", "@sbx hi", addressed=True) is None
            assert app.chat.click_choice(1, "x") is None and app.chat.approve(1) is None

    drive(scenario)


def test_channel_tail_reports_new_then_changed(seeded: SbxloopHome) -> None:
    dstore = seed_chat(seeded)
    client = MailboxClient(seeded.state_db, operator_id="brett")
    tail = ChannelTail("control")
    new, changed = tail.pull(client, now=time.time())
    assert len(new) == 2 and changed == []
    assert choice_spec(new[1]) is not None and choice_spec(new[0]) is None
    # The cursor moves only when the rows were shown: a pull a superseded
    # worker made never loses them.
    assert tail.pull(client, now=time.time())[0] == new
    tail.commit(new, changed, now=time.time())
    dstore.local_react(new[0].id, "⏳", now=time.time())
    time.sleep(0.01)
    new2, changed2 = tail.pull(client, now=time.time())
    assert new2 == [] and [r.id for r in changed2] == [new[0].id]
    tail.commit(new2, changed2, now=time.time())
    new3, changed3 = tail.pull(client, now=time.time())
    assert new3 == [] and changed3 == []
