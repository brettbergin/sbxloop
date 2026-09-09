"""Provider recovery resumes a response correction without another review."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sbxloop.engine.phases import PhaseRunner, clip_diff
from sbxloop.engine.store import StateStore
from sbxloop.errors import InvalidOutputTwice
from sbxloop.events import EventBus
from sbxloop.provider import ProviderHeldError, ProviderRecovery
from sbxloop_worker.protocol import JobRequest, JobResult
from tests.unit.test_review_response_repair import (
    MAJOR,
    ScriptedAgent,
    reply,
    review,
    runner,
    verdict,
)


class RecoveringAgent(ScriptedAgent):
    def __init__(self, responses: list[dict[str, Any]], recovery: ProviderRecovery) -> None:
        super().__init__(responses)
        self.recovery = recovery

    def submit(
        self, job: JobRequest, *, agent: str | None = None, tool_handler: Any = None
    ) -> JobResult:
        def transport(actual: JobRequest) -> JobResult:
            return super(RecoveringAgent, self).submit(
                actual, agent=agent, tool_handler=tool_handler
            )

        return self.recovery.submit(job, transport, EventBus())


def persisted_runner(agent: ScriptedAgent, workspace: Path, store: StateStore) -> PhaseRunner:
    phases = runner(agent, workspace)
    phases.store = store
    return phases


def throttled() -> dict[str, Any]:
    return {
        "status": "error",
        "session_id": "interrupted-repair",
        "usage": {"input_tokens": 7},
        "turns": 1,
        "error": {
            "type": "ProviderFailure",
            "message": "Rate limited",
            "provider": {
                "backend": "claude",
                "category": "throttle",
                "reason": "Rate limited",
                "partial_progress": True,
            },
        },
    }


@pytest.mark.parametrize("held_attempt", [1, 2])
def test_cooldown_resumes_the_same_repair_after_reopening_store(
    tmp_path: Path, held_attempt: int
) -> None:
    database = tmp_path / "state.db"
    store = StateStore(database)
    now = [1000.0]
    manager = ProviderRecovery(store, "claude", clock=lambda: now[0], jitter=lambda: 0)
    invalid = verdict({**MAJOR, "category": "unsupported"})
    responses = [reply(invalid, session_id="review", usage={"input_tokens": 100}, turns=8)]
    if held_attempt == 2:
        responses.append(
            reply(invalid, session_id="repair-one", usage={"input_tokens": 20}, turns=1)
        )
    responses.extend(
        [
            throttled(),
            reply(
                verdict(MAJOR), session_id="interrupted-repair", usage={"input_tokens": 30}, turns=1
            ),
        ]
    )
    agent = RecoveringAgent(responses, manager)
    try:
        with pytest.raises(ProviderHeldError):
            review(persisted_runner(agent, tmp_path, store))
        hold = manager.hold()
        assert hold is not None and hold.next_at is not None
        now[0] = hold.next_at
        store.close()
        store = StateStore(database)
        agent.recovery = ProviderRecovery(store, "claude", clock=lambda: now[0])
        resumed = persisted_runner(agent, tmp_path, store)

        result = review(resumed)

        assert result.findings[0].body == MAJOR["body"]
        assert result.findings[0].severity == "major"
        assert len(agent.jobs) == held_attempt + 2
        assert sum(job.available_tools is None for job in agent.jobs) == 1
        for job in agent.jobs[1:]:
            assert job.available_tools == [] and job.host_tools == [] and job.mcp_servers == []
        assert agent.jobs[-1].resume_session_id == "interrupted-repair"
        assert agent.jobs[-1].require_resume
        assert not agent.recovery.pending("r1")
        # Interrupted usage comes from ProviderRecovery once; replayed results
        # are already represented in the durable phase rows.
        rows = store.phase_attempts("r1")
        assert sum(row.input_tokens or 0 for row in rows) == 137 + (20 if held_attempt == 2 else 0)
        assert sum(row.turns or 0 for row in rows) == 10 + (1 if held_attempt == 2 else 0)
        assert resumed.drain_spend().usage is None
        assert not agent.responses
    finally:
        store.close()


def test_unparseable_correction_stays_spent_after_provider_recovery(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    now = [1000.0]
    manager = ProviderRecovery(store, "claude", clock=lambda: now[0], jitter=lambda: 0)
    agent = RecoveringAgent(
        [
            reply(verdict({**MAJOR, "category": "unsupported"}), usage={"input_tokens": 100}),
            {
                "status": "error",
                "usage": {"input_tokens": 5},
                "error": {"type": "ExpectedJsonMissing", "message": "No JSON response"},
            },
            throttled(),
            reply(verdict(MAJOR), usage={"input_tokens": 30}),
        ],
        manager,
    )
    try:
        with pytest.raises(ProviderHeldError):
            review(persisted_runner(agent, tmp_path, store))
        hold = manager.hold()
        assert hold is not None and hold.next_at is not None
        now[0] = hold.next_at
        result = review(persisted_runner(agent, tmp_path, store))
        assert result.findings[0].body == MAJOR["body"]
        assert len(agent.jobs) == 4
        assert sum(job.available_tools is None for job in agent.jobs) == 1
        assert sum(row.input_tokens or 0 for row in store.phase_attempts("r1")) == 142
        assert not agent.responses
    finally:
        store.close()


@pytest.mark.parametrize("binding", ["unclipped_diff", "head_without_diff"])
def test_checkpoint_is_not_reused_for_changed_code(tmp_path: Path, binding: str) -> None:
    store = StateStore(tmp_path / "state.db")
    updated = {**MAJOR, "body": "A new defect found after the code changed"}
    agent = ScriptedAgent(
        [
            reply(verdict({**MAJOR, "category": "unsupported"})),
            reply(verdict(MAJOR)),
            reply(verdict(updated)),
        ]
    )
    first_diff = "a" * 100_000 + "old" + "z" * 100_000
    second_diff = first_diff.replace("old", "new")
    assert clip_diff(first_diff, 150_000) == clip_diff(second_diff, 150_000)
    try:
        first = persisted_runner(agent, tmp_path, store)
        second = persisted_runner(agent, tmp_path, store)
        for phases, diff, head in [(first, first_diff, "a" * 40), (second, second_diff, "b" * 40)]:
            result = phases.review(
                diff=diff if binding == "unclipped_diff" else None,
                pr_number=1,
                round=1,
                tasks=[],
                history="",
                refuted=set(),
                **({"head_sha": head} if binding == "head_without_diff" else {}),
            )
        assert result.findings[0].body == updated["body"]
        assert len(agent.jobs) == 3
        assert agent.jobs[-1].available_tools is None
    finally:
        store.close()


def test_cooldown_does_not_reset_the_two_correction_limit(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    now = [1000.0]
    manager = ProviderRecovery(store, "claude", clock=lambda: now[0], jitter=lambda: 0)
    invalid = verdict({**MAJOR, "category": "unsupported"})
    agent = RecoveringAgent([reply(invalid), reply(invalid), throttled(), reply(invalid)], manager)
    try:
        with pytest.raises(ProviderHeldError):
            review(persisted_runner(agent, tmp_path, store))
        hold = manager.hold()
        assert hold is not None and hold.next_at is not None
        now[0] = hold.next_at
        with pytest.raises(InvalidOutputTwice, match="invalid"):
            review(persisted_runner(agent, tmp_path, store))
        with pytest.raises(InvalidOutputTwice, match="invalid"):
            review(persisted_runner(agent, tmp_path, store))
        assert len(agent.jobs) == 4
        assert not agent.responses
    finally:
        store.close()


def test_changed_original_request_still_fails_closed_after_cooldown(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    now = [1000.0]
    manager = ProviderRecovery(store, "claude", clock=lambda: now[0], jitter=lambda: 0)
    agent = RecoveringAgent(
        [reply(verdict({**MAJOR, "category": "unsupported"})), throttled()], manager
    )
    try:
        with pytest.raises(ProviderHeldError):
            review(persisted_runner(agent, tmp_path, store))
        hold = manager.hold()
        assert hold is not None and hold.next_at is not None
        now[0] = hold.next_at
        resumed = persisted_runner(agent, tmp_path, store)
        resumed.add_guidance("Review the newly changed acceptance criteria too")
        with pytest.raises(ProviderHeldError, match="interrupted request changed"):
            review(resumed)
        assert len(agent.jobs) == 2
    finally:
        store.close()
