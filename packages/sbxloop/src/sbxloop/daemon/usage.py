"""One run's ``agent.usage`` samples, folded: the total, a per-persona
breakdown and the models seen. Shared by the concierge's ``run_usage``
tool and the console's Phases tab so the two cannot disagree about what a
run spent — in tokens and turns; never in a currency (the backends report
none that means anything)."""

from __future__ import annotations

from typing import Any, NamedTuple

from sbxloop.daemon.discord_format import agent_model_label
from sbxloop.engine.store import StateStore
from sbxloop_worker.protocol import EventTypes, Usage

_USAGE_FIELDS = (
    "model",
    "backend",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


class RunUsage(NamedTuple):
    """One run's folded ``agent.usage`` samples."""

    total: Usage
    by_agent: dict[str, Usage]
    models: list[str]
    samples: int
    # Turns and distinct jobs per persona. ``samples`` is the run's total
    # turn count (one sample per turn); these break it down so a persona
    # that is expensive because it takes many turns is distinguishable from
    # one that is expensive because it runs many times. Required rather than
    # defaulted: a shared mutable default on a NamedTuple is a trap, and the
    # single producer always has both to hand.
    turns_by_agent: dict[str, int]
    jobs_by_agent: dict[str, int]

    @property
    def recorded(self) -> bool:
        """Did the backend actually report anything? ``Usage.merged`` keeps
        None as None, so an all-None total means "never reported" — which is
        not the same as zero and must not be shown as it."""
        return self.samples > 0 and (
            self.total.input_tokens is not None or self.total.output_tokens is not None
        )

    @property
    def model_line(self) -> str:
        """Backend + model pairs for the run, or a plain "not reported"."""
        return " + ".join(self.models) if self.models else "model not reported"


def usage_from_event(data: dict[str, Any]) -> Usage:
    """``agent.usage`` payloads carry an ``agent`` key the host adds on the
    way through (worker/client.py), and Usage forbids extras — so pick the
    fields out rather than validating the whole dict.

    Spend is deliberately absent: the Copilot SDK reports it as a per-turn
    constant of unknown unit, so folding it through ``Usage.merged`` once
    printed a fabricated figure the concierge repeated in chat as fact.
    ``Usage`` carries no such field at all now."""
    return Usage(**{k: data[k] for k in _USAGE_FIELDS if k in data})


def usage_for_run(store: StateStore, run_id: str, *, since: float = 0.0) -> RunUsage:
    """Fold a run's ``agent.usage`` events into a total and a per-persona
    breakdown. ``since`` drops samples older than an epoch stamp, which is
    how ``usage_today`` attributes tokens to the day they were spent
    rather than to the day the run started."""
    total = Usage()
    by_agent: dict[str, Usage] = {}
    models: list[str] = []
    samples = 0
    # One `agent.usage` event is one assistant turn, and turns — not
    # jobs — are what a run is billed and timed by: every turn re-sends
    # the whole session context. Counting them per persona is how "where
    # did it go?" gets an actionable answer instead of a token total.
    turns: dict[str, int] = {}
    jobs: dict[str, set[str]] = {}
    for _seq, event in store.events(run_id, type_prefix=EventTypes.AGENT_USAGE):
        if event.ts < since:
            continue
        sample = usage_from_event(event.data)
        total = total.merged(sample)
        who = str(event.data.get("agent") or "unknown")
        by_agent[who] = by_agent.get(who, Usage()).merged(sample)
        turns[who] = turns.get(who, 0) + 1
        if event.job_id:
            jobs.setdefault(who, set()).add(event.job_id)
        # Backend + model, so a GPT model served through Copilot reads
        # differently from a Claude model served from Claude. Events that
        # predate backend stamping render as `unknown`.
        if sample.model or sample.backend:
            label = agent_model_label(sample.backend, sample.model)
            if label not in models:
                models.append(label)
        samples += 1
    return RunUsage(total, by_agent, models, samples, turns, {k: len(v) for k, v in jobs.items()})


__all__ = ["RunUsage", "usage_for_run", "usage_from_event"]
