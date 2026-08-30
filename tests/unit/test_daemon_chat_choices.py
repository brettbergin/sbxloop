"""Bridge plumbing for clarifying questions with enumerable answers (#564).

The transport-free model lives in test_chat_choices.py; this file covers the
bridge seam: a ChoiceQuestion reply is posted through ``_send_choices``
(whose base implementation is numbered prose, so component-less backends are
unaffected), a click and a typed answer drive the concierge with the same
text, an unmatched prose reply is passed through untouched, and an
outstanding question expires on a deadline without anything ever blocking.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from sbxloop.daemon.chat import CHOICE_QUESTION_CAP
from sbxloop.daemon.chat_choices import Choice, ChoiceQuestion
from sbxloop.daemon.concierge import ConciergeReply
from tests.unit.test_daemon_discord import (
    BOT_USER,
    FakeConcierge,
    FakeMessage,
    FakeUser,
    make_bridge,
    wait_for,
)

QUESTION = ChoiceQuestion(
    prompt="What do you want changed?",
    choices=(
        Choice(value="the wording", label="The wording"),
        Choice(value="the layout", label="The layout", description="spacing and order"),
    ),
)


def ask(bridge: Any, client: Any, text: str, *, mid: int = 601) -> FakeMessage:
    """A control-channel message that talks to the concierge."""
    control = client.channels[42]
    msg = FakeMessage(f"<@{BOT_USER.id}> {text}", control, mid=mid, mentions=[BOT_USER])
    control.messages[mid] = msg
    bridge._handle_message(msg)
    return msg


def run_coro(bridge: Any, coro: Any) -> Any:
    """Run a bridge coroutine on the bridge's own event loop and wait."""
    return asyncio.run_coroutine_threadsafe(coro, bridge._aloop).result(timeout=10)


def run_question_turn(tmp_path: Path, name: str = "a") -> tuple[Any, Any, Any]:
    concierge = FakeConcierge(
        [
            ConciergeReply("I need one more thing.", question=QUESTION),
            ConciergeReply("filed it"),
        ]
    )
    bridge, client, _ = make_bridge(tmp_path / name, concierge=concierge)
    bridge.start()
    ask(bridge, client, "please file a thing")
    control = client.channels[42]
    assert wait_for(lambda: any("The wording" in s for s in control.sent))
    return bridge, client, concierge


class TestProseFallback:
    def test_base_send_choices_renders_numbered_prose(self, tmp_path: Path) -> None:
        bridge, client, _concierge = run_question_turn(tmp_path)
        try:
            control = client.channels[42]
            posted = next(s for s in control.sent if "The wording" in s)
            assert "1. The wording" in posted
            assert "2. The layout — spacing and order" in posted
            assert "I need one more thing." in posted
            assert "in your own words" in posted
        finally:
            bridge.close()

    def test_question_is_registered_in_memory_only(self, tmp_path: Path) -> None:
        bridge, _client, _c = run_question_turn(tmp_path)
        try:
            assert wait_for(lambda: bool(bridge._questions))
            # nothing on disk: the store knows nothing about questions
            assert not hasattr(bridge.dstore, "choice_question")
        finally:
            bridge.close()


class TestClickAndTypedAgree:
    def test_click_and_typed_reply_send_the_concierge_the_same_text(self, tmp_path: Path) -> None:
        clicked_bridge, _c1, clicked = run_question_turn(tmp_path, "click")
        try:
            assert wait_for(lambda: bool(clicked_bridge._questions))
            mid = next(iter(clicked_bridge._questions))
            assert clicked_bridge._answer_choice(mid, "the layout", "brett") is True
            assert wait_for(lambda: len(clicked.turns) == 2)
            click_text = clicked.turns[1][0]
        finally:
            clicked_bridge.close()

        typed_bridge, typed_client, typed = run_question_turn(tmp_path, "typed")
        try:
            assert wait_for(lambda: bool(typed_bridge._questions))
            ask(typed_bridge, typed_client, "2", mid=602)
            assert wait_for(lambda: len(typed.turns) == 2)
            typed_text = typed.turns[1][0]
        finally:
            typed_bridge.close()

        assert click_text == typed_text == "the layout"

    def test_clicking_does_not_need_a_typed_reply(self, tmp_path: Path) -> None:
        bridge, _client, concierge = run_question_turn(tmp_path)
        try:
            mid = next(iter(bridge._questions))
            assert bridge._answer_choice(mid, "the wording") is True
            assert wait_for(lambda: len(concierge.turns) == 2)
            assert concierge.turns[1][0] == "the wording"
            # answered questions leave the registry
            assert mid not in bridge._questions
        finally:
            bridge.close()

    def test_a_second_click_on_an_answered_question_is_refused_not_replayed(
        self, tmp_path: Path
    ) -> None:
        bridge, _client, concierge = run_question_turn(tmp_path)
        try:
            mid = next(iter(bridge._questions))
            assert bridge._answer_choice(mid, "the wording") is True
            assert wait_for(lambda: len(concierge.turns) == 2)
            assert bridge._answer_choice(mid, "the wording") is False
            time.sleep(0.1)
            assert len(concierge.turns) == 2
        finally:
            bridge.close()


class TestFreeTextStillWorks:
    def test_prose_that_matches_no_choice_is_passed_through_unchanged(self, tmp_path: Path) -> None:
        bridge, client, concierge = run_question_turn(tmp_path)
        try:
            ask(bridge, client, "here is a traceback I pasted", mid=603)
            assert wait_for(lambda: len(concierge.turns) == 2)
            assert concierge.turns[1][0] == "here is a traceback I pasted"
        finally:
            bridge.close()

    def test_typed_answer_still_works_after_the_question_expires(self, tmp_path: Path) -> None:
        bridge, client, concierge = run_question_turn(tmp_path)
        try:
            assert wait_for(lambda: bool(bridge._questions))
            for entry in bridge._questions.values():
                entry.deadline = time.monotonic() - 1.0
            assert bridge._outstanding() is None
            assert bridge._questions == {}
            ask(bridge, client, "the layout", mid=604)
            assert wait_for(lambda: len(concierge.turns) == 2)
            # no registry entry to map it, so the prose goes through as typed
            assert concierge.turns[1][0] == "the layout"
        finally:
            bridge.close()

    def test_click_after_expiry_is_refused_and_never_blocks(self, tmp_path: Path) -> None:
        bridge, _client, _c = run_question_turn(tmp_path)
        try:
            mid = next(iter(bridge._questions))
            bridge._questions[mid].deadline = time.monotonic() - 1.0
            t0 = time.monotonic()
            assert bridge._answer_choice(mid, "the layout") is False
            assert time.monotonic() - t0 < 1.0
        finally:
            bridge.close()


class TestRegistryHygiene:
    def test_registry_is_bounded(self, tmp_path: Path) -> None:
        bridge, _client, _c = run_question_turn(tmp_path)
        try:
            msg = next(iter(bridge._questions.values())).msg
            for i in range(CHOICE_QUESTION_CAP + 10):
                bridge._register_question(f"m{i}", QUESTION, msg)
            assert len(bridge._questions) <= CHOICE_QUESTION_CAP
        finally:
            bridge.close()

    def test_posting_a_question_never_waits_for_an_answer(self, tmp_path: Path) -> None:
        t0 = time.monotonic()
        bridge, _client, _c = run_question_turn(tmp_path)
        try:
            elapsed = time.monotonic() - t0
            assert elapsed < 5.0
        finally:
            bridge.close()


CLOSE_QUESTION = ChoiceQuestion(
    prompt="Close #12?",
    choices=(Choice(value="yes", label="Yes"), Choice(value="no", label="No")),
)


def _speak(
    bridge: Any,
    client: Any,
    text: str,
    *,
    mid: int,
    author: Any = None,
    reply_to: Any = None,
) -> Any:
    """A control-channel message from a named author, optionally a reply."""
    control = client.channels[42]
    msg = FakeMessage(
        f"<@{BOT_USER.id}> {text}", control, mid=mid, mentions=[BOT_USER], reply_to=reply_to
    )
    if author is not None:
        msg.author = author
    control.messages[mid] = msg
    return msg


class TestTypedAnswersReachTheRightQuestion:
    """#570 review: a typed reply must be matched to the question it answers,
    never to whichever question happened to be registered last."""

    def test_two_outstanding_questions_leave_a_bare_number_untouched(self, tmp_path: Path) -> None:
        bridge, client, _c = run_question_turn(tmp_path)
        try:
            first_id = next(iter(bridge._questions))
            asker = next(iter(bridge._questions.values())).msg
            # A second, unrelated question is registered directly (raw
            # registry state, not via the path under test).
            bridge._register_question("m2", CLOSE_QUESTION, asker)
            typed = bridge._inbound(_speak(bridge, client, "1", mid=701))
            assert bridge._choice_from_typed(typed, "1") == "1"
            # nothing was consumed: both questions are still outstanding
            assert set(bridge._questions) == {first_id, "m2"}
        finally:
            bridge.close()

    def test_a_reply_names_the_question_it_answers(self, tmp_path: Path) -> None:
        bridge, client, _c = run_question_turn(tmp_path)
        try:
            first_id = next(iter(bridge._questions))
            entry = bridge._questions[first_id]
            bridge._register_question("m2", CLOSE_QUESTION, entry.msg)
            posted = client.channels[42].messages[int(first_id)]
            typed = bridge._inbound(_speak(bridge, client, "1", mid=702, reply_to=posted))
            assert typed.reply_to_id == first_id
            assert bridge._choice_from_typed(typed, "1") == "the wording"
            # only the question that was answered left the registry
            assert set(bridge._questions) == {"m2"}
        finally:
            bridge.close()

    def test_someone_elses_question_does_not_rewrite_my_prose(self, tmp_path: Path) -> None:
        bridge, client, _c = run_question_turn(tmp_path)
        try:
            other = FakeUser(9, "dana")
            typed = bridge._inbound(_speak(bridge, client, "2", mid=703, author=other))
            assert bridge._choice_from_typed(typed, "2") == "2"
            assert len(bridge._questions) == 1
        finally:
            bridge.close()

    def test_a_reply_to_an_unrelated_message_is_not_guessed_at(self, tmp_path: Path) -> None:
        bridge, client, _c = run_question_turn(tmp_path)
        try:
            control = client.channels[42]
            stray = FakeMessage("something else", control, mid=9999)
            control.messages[9999] = stray
            typed = bridge._inbound(_speak(bridge, client, "2", mid=704, reply_to=stray))
            assert bridge._choice_from_typed(typed, "2") == "2"
        finally:
            bridge.close()

    def test_my_own_single_question_still_matches_without_a_reply(self, tmp_path: Path) -> None:
        bridge, client, _c = run_question_turn(tmp_path)
        try:
            typed = bridge._inbound(_speak(bridge, client, "2", mid=705))
            assert bridge._choice_from_typed(typed, "2") == "the layout"
            assert bridge._questions == {}
        finally:
            bridge.close()

    def test_a_question_in_another_channel_is_never_matched(self, tmp_path: Path) -> None:
        bridge, _client, _c = run_question_turn(tmp_path)
        try:
            entry = next(iter(bridge._questions.values()))
            elsewhere = replace(entry.msg, channel_id="4242")
            assert bridge._choice_from_typed(elsewhere, "2") == "2"
        finally:
            bridge.close()


class TestClickAttribution:
    def test_the_clicker_not_the_asker_owns_the_answered_turn(self, tmp_path: Path) -> None:
        bridge, _client, concierge = run_question_turn(tmp_path)
        try:
            mid = next(iter(bridge._questions))
            assert (
                bridge._answer_choice(
                    mid,
                    "the wording",
                    "Discord user `dana`",
                    author_id="9",
                    author_name="dana",
                )
                is True
            )
            assert wait_for(lambda: len(concierge.turns) == 2)
            assert concierge.turns[1][1] == "Discord user `dana`"
            assert bridge._requester_ids.get("Discord user `dana`") == "9"
        finally:
            bridge.close()

    def test_a_click_with_no_clicker_identity_keeps_the_asker(self, tmp_path: Path) -> None:
        bridge, _client, concierge = run_question_turn(tmp_path)
        try:
            mid = next(iter(bridge._questions))
            assert bridge._answer_choice(mid, "the wording") is True
            assert wait_for(lambda: len(concierge.turns) == 2)
            assert concierge.turns[1][1] == "Discord user `brett`"
        finally:
            bridge.close()


class TestLongPreamble:
    def test_a_long_preamble_is_split_instead_of_clipped(self, tmp_path: Path) -> None:
        bridge, client, _c = run_question_turn(tmp_path)
        try:
            control = client.channels[42]
            before = len(control.sent)
            asker = next(iter(bridge._questions.values())).msg
            long_text = "\n\n".join(f"paragraph {i} " + "x" * 400 for i in range(8))
            run_coro(bridge, bridge._post_choice_question(asker, long_text, QUESTION))
            posts = control.sent[before:]
            assert len(posts) > 1
            # every word of the preamble survives across the posts
            assert "paragraph 7" in "".join(posts)
            # the question itself is whole, in its own final post
            assert "1. The wording" in posts[-1]
            assert all(len(p) <= bridge.chat.max_message_chars for p in posts)
        finally:
            bridge.close()

    def test_a_short_preamble_still_rides_with_the_question(self, tmp_path: Path) -> None:
        bridge, client, _c = run_question_turn(tmp_path)
        try:
            control = client.channels[42]
            before = len(control.sent)
            asker = next(iter(bridge._questions.values())).msg
            run_coro(bridge, bridge._post_choice_question(asker, "One more thing.", QUESTION))
            posts = control.sent[before:]
            assert len(posts) == 1
            assert "One more thing." in posts[0] and "1. The wording" in posts[0]
        finally:
            bridge.close()


class TestEviction:
    def test_the_soonest_expiring_question_is_the_one_dropped(self, tmp_path: Path) -> None:
        bridge, _client, _c = run_question_turn(tmp_path)
        try:
            msg = next(iter(bridge._questions.values())).msg
            bridge._questions.clear()
            for i in range(CHOICE_QUESTION_CAP):
                bridge._register_question(f"m{i}", QUESTION, msg)
            # give one entry a far deadline: it must survive the next insert
            bridge._questions["m0"].deadline = time.monotonic() + 10_000
            bridge._register_question("newest", QUESTION, msg)
            assert len(bridge._questions) == CHOICE_QUESTION_CAP
            assert "m0" in bridge._questions
            assert "newest" in bridge._questions
        finally:
            bridge.close()
