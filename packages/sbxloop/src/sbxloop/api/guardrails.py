"""What stands between agents addressing each other and a runaway channel.

An agent's reply is prose in a shared channel, so `@another-agent` in it is
an address. Nothing about that is self-limiting: two agents that name each
other would answer back and forth for as long as the daemon is up, spending
the workspace's tokens on it. Every follow-up therefore passes
:meth:`Guardrails.admit` first, and each decision is recorded as an audit
event carrying the reason and never the message text:

- ``max_chain_depth``: how far a chain of agent-started turns may run from
  the human turn that started it;
- ``channel_turns_per_window`` / ``agent_turns_per_window``: how many agent
  turns one channel, and one agent in it, may take within ``window_s``;
- ``pair_cooldown_s``: how long one agent waits before addressing the same
  agent again;
- the channel's ``silenced_until``, which a person sets with stop or
  silence;
- :meth:`UsagePool.admit_turn`, the workspace's token budget.

The knobs live in ``[collaboration]``. The first refusal wins, so the
reason names the bound that was actually reached.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from sbxloop.daemon.usagepool import Admission
from sbxloop.log import get_logger

if TYPE_CHECKING:
    from sbxloop.api.collaboration import AgentTurnRecord, Author, CollaborationStore
    from sbxloop.config import Config

log = get_logger(__name__)

#: Why a follow-up was refused; the audit event carries one of these.
CHAIN_DEPTH = "chain_depth"
SILENCED = "silenced"
CHANNEL_RATE = "channel_rate"
AGENT_RATE = "agent_rate"
PAIR_COOLDOWN = "pair_cooldown"


class TurnBudget(Protocol):
    """The part of :class:`~sbxloop.daemon.usagepool.UsagePool` this needs."""

    def admit_turn(
        self, channel_id: str | None, agent_slug: str | None, now: float
    ) -> Admission: ...


class Guardrails:
    """Whether one agent may address another in a channel right now."""

    def __init__(
        self,
        store: CollaborationStore,
        config: Callable[[], Config],
        *,
        clock: Callable[[], float],
        pool: TurnBudget | None = None,
    ) -> None:
        self.store = store
        self._config = config
        self.clock = clock
        self.pool = pool

    def admit(
        self,
        channel_id: str,
        *,
        source: Author,
        target_slug: str,
        depth: int,
        trigger: str,
    ) -> Admission:
        """May ``source`` address ``target_slug`` in ``channel_id`` now?

        ``depth`` is the chain depth the follow-up would carry (the source
        turn's depth plus one). The decision is recorded either way, as
        ``collaboration.followup.queued`` or
        ``collaboration.followup.suppressed``.
        """
        now = self.clock()
        admission = self._decide(channel_id, source, target_slug, depth, now)
        self.store.record_followup_decision(
            channel_id,
            source_slug=source.id,
            target_slug=target_slug,
            trigger=trigger,
            depth=depth,
            admission=admission,
            now=now,
        )
        if not admission.ok:
            log.info(
                "collaboration.followup_suppressed",
                channel=channel_id,
                agent=target_slug,
                reason=admission.reason,
                depth=depth,
                trigger=trigger,
            )
        return admission

    def _decide(
        self, channel_id: str, source: Author, target_slug: str, depth: int, now: float
    ) -> Admission:
        limits = self._config().collaboration
        if depth > limits.max_chain_depth:
            return Admission(ok=False, reason=CHAIN_DEPTH)
        silenced_until = self.store.silenced_until(channel_id)
        if silenced_until is not None and silenced_until > now:
            return Admission(ok=False, reason=SILENCED, retry_at=silenced_until)
        window_start = now - limits.window_s
        recent = self.store.agent_turns_since(channel_id, window_start)
        if len(recent) >= limits.channel_turns_per_window:
            return Admission(
                ok=False, reason=CHANNEL_RATE, retry_at=self._retry_at(recent, limits.window_s)
            )
        by_target = [entry for entry in recent if target_slug in entry.targets]
        if len(by_target) >= limits.agent_turns_per_window:
            return Admission(
                ok=False, reason=AGENT_RATE, retry_at=self._retry_at(by_target, limits.window_s)
            )
        if limits.pair_cooldown_s > 0 and source.id:
            pair = [entry for entry in by_target if entry.source_slug == source.id]
            if pair:
                last = max(entry.created_at for entry in pair)
                if now - last < limits.pair_cooldown_s:
                    return Admission(
                        ok=False,
                        reason=PAIR_COOLDOWN,
                        retry_at=last + limits.pair_cooldown_s,
                    )
        if self.pool is not None:
            budget = self.pool.admit_turn(channel_id, target_slug, now)
            if not budget.ok:
                return budget
        return Admission(ok=True)

    @staticmethod
    def _retry_at(entries: list[AgentTurnRecord], window_s: float) -> float:
        """When the oldest counted turn leaves the window."""
        return min(entry.created_at for entry in entries) + window_s


__all__ = ["Admission", "Guardrails", "TurnBudget"]
