"""A channel of the local bridge on screen: the scrolling message list and
the chat form under it. The Chat screen is one of these on the control
channel; a run's Thread tab is one on the run's thread."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Static
from textual.worker import get_current_worker

from sbxloop.config import TUI_CONTROL_CHANNEL
from sbxloop.daemon.store import LocalMessage
from sbxloop.tui.chat import ChannelTail, ChatSession, choice_spec, is_addressed
from sbxloop.tui.context import console_of
from sbxloop.tui.widgets.chat_input import ChatInput

if TYPE_CHECKING:
    from sbxloop.tui.app import SbxloopTui

from sbxloop.tui.widgets.message import ApproveButton, ChoiceButton, MessageWidget

#: Rows kept on screen per channel; older ones are evicted (the store keeps them).
WIDGET_CAP = 400


class ThreadView(Vertical):
    DEFAULT_CSS = """
    ThreadView { height: 1fr; }
    ThreadView VerticalScroll { height: 1fr; }
    ThreadView #empty { color: $text-muted; padding: 1 2; }
    """

    def __init__(self, channel_id: str, *, thread: bool = False, run_id: str | None = None) -> None:
        super().__init__()
        self.channel_id = channel_id
        self.thread = thread
        self.run_id = run_id
        self.tail = ChannelTail(channel_id)
        self.widgets: dict[int, MessageWidget] = {}
        self._toasted = False

    @property
    def console_app(self) -> SbxloopTui:
        return console_of(self)

    @property
    def session(self) -> ChatSession:
        return self.console_app.chat

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="messages"):
            yield Static("nothing here yet", id="empty")
        box = ChatInput(thread=self.thread, prefix=self.console_app.config.tui.command_prefix)
        box.disabled = self.console_app.read_only
        yield box

    def on_mount(self) -> None:
        self.pull()

    # -- data --------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="thread-pull")
    def pull(self) -> None:
        app = self.console_app
        try:
            new, changed = self.tail.pull(app.mailbox, now=app.clock())
            taken = app.chat.taken()
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"chat read failed: {exc}", severity="error")
            return
        # A superseded or shut-down worker keeps running to here (a thread
        # cannot be cancelled); its rows are not applied.
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._apply, new, changed, taken)

    def _apply(self, new: list[LocalMessage], changed: list[LocalMessage], taken: set[int]) -> None:
        try:
            scroller = self.query_one("#messages", VerticalScroll)
        except NoMatches:
            return  # the view is being torn down
        at_end = scroller.scroll_y >= scroller.max_scroll_y - 2
        first = not self.widgets
        app = self.console_app
        self.tail.commit(new, changed, now=app.clock())
        # A row is mounted once, whatever a racing pull reported: the
        # widget map is the authority, not the cursor.
        fresh = [r for r in new if r.id not in self.widgets]
        if fresh:
            with contextlib.suppress(Exception):
                empty = self.query_one("#empty", Static)
                if self.tail.skipped:
                    empty.update(f"… {self.tail.skipped} older row(s) not shown")
                else:
                    empty.remove()
        mounted: list[MessageWidget] = []
        for row in fresh:
            widget = MessageWidget(row, clock=app.clock)
            self.widgets[row.id] = widget
            mounted.append(widget)
        if mounted:
            scroller.mount(*mounted)
        for row in [*changed, *(r for r in new if r.id not in {f.id for f in fresh})]:
            known = self.widgets.get(row.id)
            if known is not None:
                known.show(row)
        for row_id in taken:
            own = self.widgets.get(row_id)
            if own is not None and own.pending:
                own.show(own.row._replace(taken_at=app.clock()))
        # The screen keeps a window of rows, not the channel's history:
        # the oldest go once the window overflows (they stay in the store).
        while len(self.widgets) > WIDGET_CAP:
            oldest = next(iter(self.widgets))
            self.widgets.pop(oldest).remove()
            self.tail.skipped += 1
        if fresh and (at_end or first):
            scroller.scroll_end(animate=False)
        if fresh and self.channel_id == TUI_CONTROL_CHANNEL:
            app.chat_seen(max(r.id for r in fresh))

    # -- input -------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        box = event.input
        if not isinstance(box, ChatInput):
            return
        text, addressed, reply = box.take()
        if not text:
            return
        unaddressed = not addressed and reply is None and not is_addressed(text, prefix=box.prefix)
        if unaddressed and not self._toasted:
            self._toasted = True
            self.app.notify(
                "not addressed to the bot — it is left alone; "
                "prefix @sbx, press ctrl+t, or reply (r)",
                severity="information",
                timeout=6,
            )
        row_id = self.session.send(self.channel_id, text, addressed=addressed, reply_to_id=reply)
        if row_id is None:
            self.app.notify("read-only: nothing is sent", severity="warning")
            return
        self.pull()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button
        if isinstance(button, ChoiceButton):
            self._answer(button.row_id, button.value)
        elif isinstance(button, ApproveButton):
            if self.session.approve(button.row_id) is None:
                self.app.notify("read-only: nothing is sent", severity="warning")
            else:
                self.app.notify("approval sent — the daemon lands the PR")
                self.pull()
        else:
            return
        event.stop()

    def _answer(self, question_id: int, value: str) -> None:
        if self.session.click_choice(question_id, value) is None:
            self.app.notify("read-only: nothing is sent", severity="warning")
            return
        self.app.notify(f"answered: {value}")
        self.pull()

    def reply_to_focused(self) -> None:
        """``r``: reply to the newest bot row (the question just asked)."""
        latest = next(
            (w.row.id for w in reversed(self.widgets.values()) if w.row.direction == "out"), None
        )
        if latest is None:
            return
        box = self.query_one(ChatInput)
        box.set_reply(latest)
        box.focus()

    def pick(self, index: int) -> bool:
        """``1``-``5``: answer the newest question still open — never an
        older one by accident. False when no question is open."""
        now = self.console_app.clock()
        for widget in reversed(list(self.widgets.values())):
            spec = choice_spec(widget.row)
            if spec is None or not spec.open(now):
                continue
            value = spec.value_for(index)
            if value is None:
                self.app.notify(f"that question has no option {index}", severity="warning")
            else:
                self._answer(widget.row.id, value)
            return True
        return False
