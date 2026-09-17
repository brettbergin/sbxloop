"""The workspace budget pool: what every run and every chat turn may spend
in one calendar day, and whether the next one may start.

Two daily limits share one day, the one ``[daemon] run_cap_timezone``
names (the same boundary the run cap has always used):

* the run cap, ``[daemon] max_runs_per_day``, counted exactly as the loop
  counts it (fresh starts plus resumes); it applies to runs only;
* the token budget, ``[daemon] daily_token_budget``: input plus output
  tokens reported by runs *and* chat turns since the day began. Unset, it
  never refuses. Cache figures are recorded but are not budget.

Charges land in ``workspace_usage``: a run's ``agent.usage`` events through
:meth:`UsagePool.subscriber` on the run's bus, and a chat turn's reported
usage through :meth:`UsagePool.charge`, which the concierge calls once a
turn's job returns. A refusal carries ``retry_at``, the next day boundary.

State lives in the database only, so any number of pools over one store
(the loop's, the concierge's) agree.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sbxloop.daemon.usage import usage_from_event
from sbxloop.log import get_logger
from sbxloop_worker.protocol import Event, EventTypes, Usage

if TYPE_CHECKING:
    from sbxloop.config import Config
    from sbxloop.daemon.model import WorkItem
    from sbxloop.daemon.store import DaemonStore
    from sbxloop.db.daemon_models import WorkspaceUsageRow

log = get_logger(__name__)

UsageSource = Literal["run", "turn"]
#: The reasons this pool refuses with. ``Admission.reason`` is any string
#: under the shared admission contract, so other limits can add their own.
RefusalReason = Literal["run_cap", "token_budget"]


@dataclass(frozen=True)
class Admission:
    """Whether a run or a turn may start now; when not, why and when to ask
    again."""

    ok: bool
    reason: str | None = None
    retry_at: float | None = None


def fairness_key(item: WorkItem) -> str:
    """Whose turn a queued item is, for sharing run slots.

    The channel an item was asked from, once work items carry one; until
    then whoever requested it, and for an item nobody requested by name the
    source it came from (the prefix of its typed id: ``gh``, ``chat``,
    ``api``, ``sched``)."""
    channel = getattr(item, "channel_id", None)
    if isinstance(channel, str) and channel:
        return f"channel:{channel}"
    if item.requested_by:
        return f"requester:{item.requested_by}"
    return f"source:{item.item_id.split(':', 1)[0]}"


class UsagePool:
    """Charges and admission over the daemon's store."""

    def __init__(
        self,
        dstore: DaemonStore,
        config: Callable[[], Config],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.dstore = dstore
        self._config = config
        self.clock = clock

    # -- the day ---------------------------------------------------------------

    def day(self, now: float) -> tuple[float, float]:
        """``(start, next_start)`` of the pool's calendar day holding ``now``."""
        from sbxloop.daemon.loop import day_window

        return day_window(now, self._config().daemon.run_cap_timezone)

    # -- charging ----------------------------------------------------------------

    def charge(
        self,
        *,
        source: UsageSource,
        ref_id: str,
        agent_slug: str | None,
        channel_id: str | None,
        usage: Usage | None,
    ) -> None:
        """Record one usage sample against today. A sample that reported
        nothing at all charges nothing."""
        if usage is None:
            return
        figures = (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
        )
        if all(value is None for value in figures):
            return
        inp, out, cache_read, cache_write = (max(0, value or 0) for value in figures)
        self.dstore.record_usage(
            ts=self.clock(),
            source=source,
            ref_id=ref_id,
            agent_slug=agent_slug,
            channel_id=channel_id,
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    def subscriber(self, channel_id: str | None = None) -> Callable[[Event], None]:
        """A run bus subscriber that charges each ``agent.usage`` event to
        the run it belongs to. Failures are logged, never raised: spend
        accounting must not fail a run."""

        def on_event(event: Event) -> None:
            if event.type != EventTypes.AGENT_USAGE:
                return
            # The assigned agent's slug when the run has an assignment,
            # else the run role the event names.
            slug = event.data.get("agent_slug") or event.data.get("agent")
            try:
                self.charge(
                    source="run",
                    ref_id=event.run_id,
                    agent_slug=slug if isinstance(slug, str) and slug else None,
                    channel_id=channel_id,
                    usage=usage_from_event(event.data),
                )
            except Exception:
                log.warning("usage_pool.charge_failed", run=event.run_id, exc_info=True)

        return on_event

    # -- admission -----------------------------------------------------------------

    def admit_tokens(self, now: float) -> Admission:
        """May anything spend tokens now? Only the token budget applies. The
        loop asks this on its own for work the run cap exempts."""
        return self._tokens_refusal(now) or Admission(ok=True)

    def _tokens_refusal(self, now: float) -> Admission | None:
        budget = self._config().daemon.daily_token_budget
        if budget is None:
            return None
        start, next_start = self.day(now)
        spent = sum(self.dstore.usage_tokens_since(start, next_start).values())
        if spent < budget:
            return None
        return Admission(ok=False, reason="token_budget", retry_at=next_start)

    def admit_run(self, item: WorkItem | None, now: float) -> Admission:
        """May a run start now? ``item`` is the candidate when one is known
        (the loop asks before it picks one); today every run draws on the
        same workspace limits."""
        cap = self._config().daemon.max_runs_per_day
        start, next_start = self.day(now)
        if self.dstore.runs_started_since(start) >= cap:
            return Admission(ok=False, reason="run_cap", retry_at=next_start)
        return self._tokens_refusal(now) or Admission(ok=True)

    def admit_turn(self, channel_id: str | None, agent_slug: str | None, now: float) -> Admission:
        """May a chat turn start now? Only the token budget applies."""
        return self.admit_tokens(now)

    # -- reading -------------------------------------------------------------------

    def tokens_today(self, now: float) -> int:
        start, next_start = self.day(now)
        return sum(self.dstore.usage_tokens_since(start, next_start).values())

    def snapshot(self, now: float) -> dict[str, Any]:
        """Today's figures: the day, runs against the cap, tokens against the
        budget by source."""
        daemon = self._config().daemon
        start, next_start = self.day(now)
        by_source = self.dstore.usage_tokens_since(start, next_start)
        return {
            "day_start": start,
            "resets_at": next_start,
            "runs_today": self.dstore.runs_started_since(start),
            "max_runs_per_day": daemon.max_runs_per_day,
            "tokens_today": sum(by_source.values()),
            "daily_token_budget": daemon.daily_token_budget,
            "runs_tokens_today": by_source.get("run", 0),
            "turns_tokens_today": by_source.get("turn", 0),
        }

    def entries(self, *, since: float) -> list[WorkspaceUsageRow]:
        """Every charge at or after ``since``, oldest first."""
        return self.dstore.usage_entries_since(since)


__all__ = ["Admission", "RefusalReason", "UsagePool", "UsageSource", "fairness_key"]
