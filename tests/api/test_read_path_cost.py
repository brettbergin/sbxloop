"""What the chat UI's polling costs the store's one lock.

Every read the browser makes runs on the API executor under the daemon
store's single process-wide lock, so a read whose query count grows with
the channel's history serialises against the daemon's own writes. These
are the bounds, asserted as statement counts rather than as timings: a
regression here shows up in the field as a slow daemon, not as a wrong
answer, and nothing else in the suite would catch it.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event

from sbxloop.api import work_delivery
from sbxloop.daemon.concierge import ConciergeReply
from sbxloop.daemon.model import WorkItem
from sbxloop.ghids import chat_item_id
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled


class NumberedCodeConcierge:
    """One code ask per turn, each naming a different issue, so the turns
    a scan does or does not reach are told apart by their links."""

    def __init__(self) -> None:
        self.issue = 100

    def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
        self.issue += 1
        kwargs["on_code_work"]("owner/repo", self.issue, f"Issue {self.issue}")
        future: Future[ConciergeReply] = Future()
        future.set_result(ConciergeReply("Queued the requested issue."))
        return future


@contextmanager
def statements(api: Any) -> Iterator[list[str]]:
    """Every SQL statement the store executes inside the block."""
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        seen.append(" ".join(sql.split()))

    engine = api.harness.dstore._engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def touching(seen: list[str], table: str) -> int:
    return sum(1 for sql in seen if table in sql)


def _channel(api: Any, headers: dict[str, str]) -> str:
    return str(api.client.post("/v1/channels", json={}, headers=headers).json()["id"])


def _asks(api: Any, headers: dict[str, str], channel: str, count: int) -> None:
    """``count`` turns in ``channel``, each with a workload item keyed by
    the message that asked for it."""
    for index in range(count):
        accepted = api.client.post(
            f"/v1/channels/{channel}/turns",
            headers=headers,
            json={"content": f"Prepare report {index}", "target_slugs": ["concierge"]},
        ).json()
        assert api.ctx.turns.wait_idle(timeout=5)
        key = accepted["turn"]["input_message_id"]
        api.harness.dstore.upsert_new(
            WorkItem(
                item_id=chat_item_id(key),
                source_key=key,
                title=f"Report {index}",
                body="Prepare a report",
                kind="workload",
            ),
            api.clock(),
        )


def test_projecting_a_channel_s_work_costs_the_same_at_any_depth(api: Any) -> None:
    """The items behind a channel's links are read in one query, not one
    per link: the cost of the messages page must not grow with how much
    work the conversation has asked for."""
    api.ctx.concierge = FakeConcierge()
    headers = bearer(register(api))
    shallow, deep = _channel(api, headers), _channel(api, headers)
    _asks(api, headers, shallow, 1)
    _asks(api, headers, deep, 6)

    with statements(api) as seen:
        assert len(api.ctx.project_work(shallow)) == 1
        few = touching(seen, "daemon_work_items")
    with statements(api) as seen:
        assert len(api.ctx.project_work(deep)) == 6
        many = touching(seen, "daemon_work_items")
    assert few == many, f"{few} statements for one link, {many} for six"


def test_code_links_read_only_the_recent_turns_of_the_channel(api: Any, monkeypatch: Any) -> None:
    """``_code_links`` walks the channel's turns to find the issues its
    agents named. That walk is bounded to the same window the messages
    page carries, so a long-lived channel does not re-read and re-parse
    its whole history on every poll."""
    api.ctx.concierge = NumberedCodeConcierge()
    headers = bearer(register(api))
    channel = _channel(api, headers)
    turns: list[str] = []
    for index in range(3):
        accepted = api.client.post(
            f"/v1/channels/{channel}/turns",
            headers=headers,
            json={"content": f"Add feature {index}", "intent": "code"},
        ).json()
        settled(api.client, headers, channel, accepted["turn"]["id"])
        turns.append(accepted["turn"]["id"])
        # Turns are ordered by when they were taken; the frozen test clock
        # would otherwise make "the most recent two" a matter of id order.
        api.clock.t += 10
    monkeypatch.setattr(work_delivery, "CODE_LINK_TURNS", 2)
    projected = {snapshot["turn_id"] for snapshot in api.ctx.project_work(channel)}
    assert projected == set(turns[-2:])


def test_a_turn_s_history_reads_its_authors_in_one_query(api: Any) -> None:
    """One query for the distinct people who wrote the page, not one per
    author: the history is rebuilt on every turn the channel takes."""
    from tests.api.test_workspace_members import _join, _owner_id

    headers = bearer(register(api))
    store = api.ctx.collaboration
    channel = _channel(api, headers)
    people = [_owner_id(api)]
    for name in ("ana", "bo", "cy"):
        _, user_id = _join(api, "member", name)
        added = api.client.post(
            f"/v1/channels/{channel}/members",
            headers=headers,
            json={"user_id": user_id, "role": "member"},
        )
        assert added.status_code == 201, added.text
        people.append(user_id)
    turn = None
    for index, user_id in enumerate(people):
        turn, _message, _ = store.accept_turn(
            user_id,
            channel,
            content=f"message {index}",
            targets=(),
            client_turn_id=None,
            client_message_id=None,
            actor=None,
            now=api.clock() + index,
        )
    assert turn is not None

    with statements(api) as seen:
        history = store.turn_history(turn)
        reads = touching(seen, "collaboration_users")
    assert reads == 1, f"{reads} queries for {len(people)} authors"
    # Every prior turn is in the history, each naming the person who wrote
    # it: the batching answers the same question, not a cheaper one.
    assert sum(1 for user_id in people if user_id in history) == len(people) - 1
