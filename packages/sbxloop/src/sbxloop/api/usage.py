"""Usage as observed telemetry: tokens and turns a backend reported, never
a provider invoice.

One run's figure is the same fold the concierge's ``run_usage`` tool and
the console show (:func:`sbxloop.daemon.usage.usage_for_run`); a window is
that fold over every run touched in it, attributed to the samples' own
timestamps. Missing values stay missing: a backend that reports no cache
figures, or none at all, reads as ``null``, and ``recorded`` says whether
anything was reported — which is not the same as zero. No currency
appears anywhere; ``spend`` is ``null`` with the basis stated, so a client
cannot mistake token totals for a bill.
"""

from __future__ import annotations

from collections.abc import Callable

from sbxloop.api.models import (
    RunUsage as RunUsageOut,
    UsageAgent,
    UsageTotals,
    UsageWindow,
    UsageWindowRun,
    rfc3339,
)
from sbxloop.api.publicids import run_public_id
from sbxloop.daemon.usage import SPEND_NOT_REPORTED, RunUsage, usage_for_run
from sbxloop.engine.model import RunRecord
from sbxloop.engine.store import StateStore
from sbxloop_worker.protocol import Usage

#: How wide a usage window may be, so a fold stays bounded.
WINDOW_MAX_S = 90 * 86400.0


def totals(usage: Usage) -> UsageTotals:
    return UsageTotals(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )


def run_usage(store: StateStore, record: RunRecord, *, since: float = 0.0) -> RunUsageOut:
    folded: RunUsage = usage_for_run(store, record.run_id, since=since)
    return RunUsageOut(
        run_id=run_public_id(record.run_id),
        recorded=folded.recorded,
        turns=folded.samples,
        models=list(folded.models),
        total=totals(folded.total),
        by_agent=[
            UsageAgent(
                agent=agent,
                usage=totals(usage),
                turns=folded.turns_by_agent.get(agent, 0),
                jobs=folded.jobs_by_agent.get(agent, 0),
            )
            for agent, usage in folded.by_agent.items()
        ],
        by_phase_model=(
            {label: totals(usage) for label, usage in folded.by_phase_model.items()}
            if folded.by_phase_model
            else {}
        ),
        spend=None,
        spend_basis=SPEND_NOT_REPORTED,
    )


def window_usage(
    store: StateStore,
    *,
    since: float,
    until: float,
    now: float,
    runs: Callable[[], list[RunRecord]],
) -> UsageWindow:
    """Every run touched in ``[since, until)``, folded; samples outside the
    window are left out, so a run that straddles it counts what it spent
    inside."""
    rows: list[UsageWindowRun] = []
    total = Usage()
    turns = 0
    models: list[str] = []
    considered = 0
    for record in runs():
        if record.updated_at < since or record.created_at >= until:
            continue
        considered += 1
        folded = usage_for_run(store, record.run_id, since=since)
        if not folded.recorded:
            rows.append(
                UsageWindowRun(
                    run_id=run_public_id(record.run_id),
                    kind=record.kind,
                    state=record.state,
                    recorded=False,
                    turns=0,
                    total=totals(Usage()),
                )
            )
            continue
        total = total.merged(folded.total)
        turns += folded.samples
        models.extend(m for m in folded.models if m not in models)
        rows.append(
            UsageWindowRun(
                run_id=run_public_id(record.run_id),
                kind=record.kind,
                state=record.state,
                recorded=True,
                turns=folded.samples,
                total=totals(folded.total),
            )
        )
    return UsageWindow(
        since=rfc3339(since) or "",
        until=rfc3339(until) or "",
        observed_at=rfc3339(now) or "",
        runs=rows,
        runs_considered=considered,
        runs_recorded=sum(1 for r in rows if r.recorded),
        turns=turns,
        models=models,
        total=totals(total),
        spend=None,
        spend_basis=SPEND_NOT_REPORTED,
    )


def parse_when(value: str | None, *, default: float) -> float:
    """An RFC 3339 timestamp or an epoch number; ``default`` when absent."""
    from datetime import datetime

    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        pass
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError as exc:
        raise ValueError(f"not a timestamp: {value!r}") from exc


__all__ = ["WINDOW_MAX_S", "parse_when", "run_usage", "totals", "window_usage"]
