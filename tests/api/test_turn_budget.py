"""A person's chat turn against the workspace's daily token budget.

Every turn is charged to the pool, so a turn a person starts has to pass
the same admission a turn one agent starts for another already does: once
the day's budget is spent, the turn is refused before anything is sent to
the model, the person is told why in the channel, and the turn settles as
failed. With room in the budget, or with no budget configured, a person's
turn is unchanged.

Expected values come from the configured budget and the scripted replies,
never from the code under test.
"""

from __future__ import annotations

from typing import Any

from tests.api.conftest import build
from tests.api.test_agent_mentions import ScriptedConcierge, _ask, _channel, _turns
from tests.api.test_collaboration import bearer, register


def _api(tmp_path: Any, budget: int | None) -> Any:
    daemon: dict[str, Any] = {} if budget is None else {"daily_token_budget": budget}
    return build(tmp_path, config={"daemon": daemon})


def _spend(api: Any, tokens: int) -> None:
    api.loop.dstore.record_usage(
        ts=api.clock(),
        source="run",
        ref_id="spent",
        agent_slug=None,
        channel_id=None,
        input_tokens=tokens,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )


def _messages(api: Any, headers: dict[str, str], channel: str) -> list[dict[str, Any]]:
    page = api.client.get(f"/v1/channels/{channel}/messages", headers=headers)
    assert page.status_code == 200, page.text
    return list(page.json())


def test_a_persons_turn_is_refused_once_the_daily_budget_is_spent(tmp_path: Any) -> None:
    """The budget is spent before the person asks. Nothing reaches the
    model, the turn settles as failed with the reason, and the channel
    carries the refusal as the turn's error message, naming the budget
    and when it resets."""
    api = _api(tmp_path, budget=100)
    with api.client:
        concierge = ScriptedConcierge({"planner": "a plan"})
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _spend(api, 150)

        _ask(api, headers, channel, "@planner plan the bake")

        assert concierge.calls == [], concierge.calls
        (turn,) = _turns(api, headers, channel)
        assert turn["status"] == "failed", turn
        assert "token budget" in (turn["error"] or "")
        assert "150/100" in turn["error"]
        assert "00:00 UTC" in turn["error"]
        errors = [m for m in _messages(api, headers, channel) if m["kind"] == "turn_error"]
        assert [m["content"] for m in errors] == [turn["error"]]
    api.ctx.close()


def test_a_persons_turn_runs_while_the_budget_has_room(tmp_path: Any) -> None:
    api = _api(tmp_path, budget=100)
    with api.client:
        concierge = ScriptedConcierge({"planner": "a plan"})
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _spend(api, 50)

        _ask(api, headers, channel, "@planner plan the bake")

        assert len(concierge.calls) == 1, concierge.calls
        (turn,) = _turns(api, headers, channel)
        assert turn["status"] == "completed", turn
    api.ctx.close()


def test_without_a_budget_a_persons_turn_is_never_refused(tmp_path: Any) -> None:
    api = _api(tmp_path, budget=None)
    with api.client:
        concierge = ScriptedConcierge({"planner": "a plan"})
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _spend(api, 10_000_000)

        _ask(api, headers, channel, "@planner plan the bake")

        assert len(concierge.calls) == 1, concierge.calls
        (turn,) = _turns(api, headers, channel)
        assert turn["status"] == "completed", turn
    api.ctx.close()
