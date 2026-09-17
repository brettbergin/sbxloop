"""Work a chat turn starts carries the channel and the agents the turn asked for.

The turn's caller says which channel the turn belongs to, which agent leads
and which agent the person asked for in each run role. A workload the turn
queues carries all three; an issue the turn files or labels leaves a note the
polled item picks up. A turn that names none of them queues work exactly as
before.
"""

from __future__ import annotations

import json
from pathlib import Path

from sbxloop.daemon.model import WorkItem
from tests.unit.test_daemon_concierge import FakeGithub, make


def test_a_chat_workload_carries_the_channel_and_the_agents(tmp_path: Path) -> None:
    concierge, _, _, _, dstore = make(
        tmp_path, [{"calls": [("start_workload", {"ask": "Bake bread"})], "text": "queued"}]
    )
    concierge.submit_turn(
        "bake",
        author="ana",
        message_id="m1",
        channel_id="chn_kitchen",
        work_lead="concierge",
        work_roles={"planner": "baker"},
    ).result(timeout=10)
    item = dstore.get("chat:m1")
    assert item is not None
    assert item.channel_id == "chn_kitchen"
    assert item.lead_agent == "concierge"
    assert item.assignment_json is not None
    assert json.loads(item.assignment_json)["roles"] == {"planner": "baker"}


def test_a_turn_that_names_nothing_queues_as_before(tmp_path: Path) -> None:
    concierge, _, _, _, dstore = make(
        tmp_path, [{"calls": [("start_workload", {"ask": "Bake bread"})], "text": "queued"}]
    )
    concierge.submit_turn("bake", author="ana", message_id="m2").result(timeout=10)
    item = dstore.get("chat:m2")
    assert item is not None
    assert (item.channel_id, item.lead_agent, item.assignment_json) == (None, None, None)


def test_code_work_leaves_a_note_the_polled_issue_carries(tmp_path: Path) -> None:
    concierge, _, _, _, dstore = make(
        tmp_path,
        [
            {
                "calls": [("create_issue", {"title": "Add retries", "body": "Wrap fetch()."})],
                "text": "filed",
            },
            {"calls": [("label_issue_for_run", {"number": 12})], "text": "labelled"},
        ],
        github=FakeGithub(),
    )
    for text in ("add retries", "run #12"):
        concierge.submit_turn(
            text,
            author="ana",
            channel_id="chn_code",
            work_roles={"builder": "smith"},
        ).result(timeout=10)
    for number in ("41", "12"):
        dstore.upsert_new(
            WorkItem(
                item_id=f"gh:issue:{number}",
                source_key=number,
                title="t",
                repo="owner/repo",
            ),
            1.0,
        )
        item = dstore.get(f"gh:issue:{number}")
        assert item is not None
        assert item.channel_id == "chn_code"
        assert item.assignment_json is not None
        assert json.loads(item.assignment_json)["roles"] == {"builder": "smith"}
