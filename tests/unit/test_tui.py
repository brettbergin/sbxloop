"""TUI unit tests: the chat input form, chat event rendering, and the
dashboard's chat lifecycle."""

from __future__ import annotations

from rich.console import Console

from sbxloop.cli.tui import ChatInput, Dashboard, format_event, render_event
from sbxloop.events import HostEventTypes
from sbxloop_worker.protocol import Event


def make_event(type: str, **data: object) -> Event:
    return Event.now(type, "r1", **data)


def render_to_text(renderable: object) -> str:
    console = Console(record=True, width=100)
    console.print(renderable)
    return console.export_text()


class TestChatInput:
    def collect(self) -> tuple[ChatInput, list[str]]:
        submitted: list[str] = []
        return ChatInput(submitted.append), submitted

    def test_enter_submits_stripped_line(self) -> None:
        chat, submitted = self.collect()
        chat.feed(b"  fix the tests  \r")
        assert submitted == ["fix the tests"]
        assert chat.buffer == ""

    def test_empty_line_not_submitted(self) -> None:
        chat, submitted = self.collect()
        chat.feed(b"\r\n   \r")
        assert submitted == []

    def test_backspace_and_ctrl_u(self) -> None:
        chat, submitted = self.collect()
        chat.feed(b"abcd\x7f\x7f")
        assert chat.buffer == "ab"
        chat.feed(b"\x15")
        assert chat.buffer == ""
        chat.feed(b"xy\x08\n")
        assert submitted == ["x"]

    def test_escape_sequences_swallowed(self) -> None:
        chat, submitted = self.collect()
        # Up arrow (CSI A), Delete (CSI 3~), then real text.
        chat.feed(b"\x1b[A\x1b[3~hello\r")
        assert submitted == ["hello"]

    def test_bare_escape_costs_at_most_the_next_char(self) -> None:
        chat, submitted = self.collect()
        # Esc alone, later typing: only the char right after Esc is treated
        # as an Alt-combo and dropped; the rest goes through.
        chat.feed(b"\x1b")
        chat.feed(b"xhello\r")
        assert submitted == ["hello"]

    def test_utf8_split_across_feeds(self) -> None:
        chat, submitted = self.collect()
        encoded = "héllo ✓".encode()
        chat.feed(encoded[:3])
        chat.feed(encoded[3:])
        chat.feed(b"\r")
        assert submitted == ["héllo ✓"]

    def test_renderable_shows_hint_then_buffer(self) -> None:
        chat, _ = self.collect()
        assert "type to chat" in render_to_text(chat.renderable())
        chat.feed(b"do the thing")
        assert "do the thing" in render_to_text(chat.renderable())


class TestChatRendering:
    def test_chat_message_renders_as_user_panel(self) -> None:
        rendered = render_event(
            make_event(HostEventTypes.CHAT_MESSAGE, message_id="m1", text="switch to Go")
        )
        text = render_to_text(rendered)
        assert "you" in text
        assert "switch to Go" in text

    def test_chat_reply_renders_markdown(self) -> None:
        rendered = render_event(
            make_event(HostEventTypes.CHAT_REPLY, message_id="m1", reply="**on it**")
        )
        text = render_to_text(rendered)
        assert "agent" in text
        assert "on it" in text

    def test_chat_reply_error_renders_red_panel(self) -> None:
        rendered = render_event(
            make_event(HostEventTypes.CHAT_REPLY, message_id="m1", error="worker died")
        )
        assert "steering failed: worker died" in render_to_text(rendered)

    def test_chat_action_renders_one_liner(self) -> None:
        rendered = render_event(
            make_event(
                HostEventTypes.CHAT_ACTION,
                action="steer_run",
                guidance="g",
                message="user steering: standing guidance added — g",
            )
        )
        assert "standing guidance added" in render_to_text(rendered)

    def test_format_event_carries_chat_text(self) -> None:
        line = format_event(make_event(HostEventTypes.CHAT_MESSAGE, text="hello agent"))
        assert "chat.message" in line
        assert "hello agent" in line
        line = format_event(make_event(HostEventTypes.CHAT_REPLY, reply="hi"))
        assert "hi" in line


class TestDashboardChat:
    def test_pending_then_processing_then_cleared(self) -> None:
        dashboard = Dashboard()
        dashboard.post_chat("m1", "please hurry")
        assert "please hurry" in render_to_text(dashboard.renderable())
        assert "queued" in render_to_text(dashboard.renderable())

        dashboard.on_event(
            make_event(HostEventTypes.CHAT_MESSAGE, message_id="m1", text="please hurry")
        )
        assert dashboard.chat_pending == {}
        assert dashboard.chat_processing == "please hurry"
        assert "steering" in render_to_text(dashboard.renderable())

        dashboard.on_event(make_event(HostEventTypes.CHAT_REPLY, message_id="m1", reply="ok"))
        assert dashboard.chat_processing is None
        text = render_to_text(dashboard.renderable())
        assert "steering" not in text
        assert "queued" not in text

    def test_chat_line_rendered_inside_panel(self) -> None:
        dashboard = Dashboard()
        chat = ChatInput(lambda _: None)
        chat.feed(b"typing here")
        assert "typing here" in render_to_text(dashboard.renderable(chat.renderable()))
