"""What the agent is told about the loop it is running inside.

Every agent session in this codebase is one stage of a run, and each stage
learned its situation from whatever its own prompt happened to restate: the
builder was told about the workspace, the reviewer about being read-only,
the concierge about the daemon — four partial accounts of one machine, each
drifting on its own. A stage that misreads its situation burns a revision
budget on it: writing a file outside the workspace and losing it, treating a
blocked domain as a flake and retrying, or reporting work it only planned.

So the situation is said once, here, and composed onto every session's
system message by ``PhaseRunner._agent_job`` and the concierge. The system
message is the right home for it rather than the prompt: it is identical
across every turn of every stage, so it caches, where the same text in a
phase prompt is re-sent with each turn — ``AGENTS.md`` goal 3, "spend scales
with turns, not jobs".

The block is deliberately **domain-neutral**: it describes the loop's shape,
never a language, a toolchain or an incident, and it carries no issue
numbers and no paths into this codebase (``scripts/check_self_references.py``
is the gate). What the *target* repository says about itself travels a
different road — ``engine.repocontext`` reads its instruction files into
``$repo_conventions``, and those outrank everything written here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from sbxloop.config import Config

__all__ = ["ROLE_BY_PHASE", "Role", "brief_for_phase", "harness_context"]

#: What a session is *for*. The head of the briefing is the same for all of
#: them; the tail is what this role may do, and a role that must not modify
#: anything is told so here as well as by its prompt.
Role = Literal["planner", "builder", "critic", "operator", "concierge"]

#: Which role each prompt name runs as. Keyed by the same strings as
#: ``phases.AGENT_NAMES``, so a new phase that forgets a role fails the
#: parity test rather than silently getting the builder's briefing.
ROLE_BY_PHASE: dict[str, Role] = {
    "decompose": "planner",
    "build": "builder",
    "steer": "planner",
    "review": "critic",
    "operator_plan": "operator",
    "operator_execute": "operator",
    "operator_judge": "critic",
}

_HEAD = """\
# Where you are

You are one stage of an automated engineering loop. The loop carries a
single ask — a chat message, or an issue someone labelled — from that ask to
a delivered result, unattended, with a person able to watch it and steer it
from chat at any point. You are one session in that sequence.

Each stage is its own session with its own brief. You do not see the other
stages' transcripts, and this session's own scratch does not survive it.
Exactly two things carry forward: what you leave in the workspace, and what
you report at the end of this turn. Work you did but did not report did not
happen, as far as the next stage is concerned.

You are inside an isolated microVM. The sandbox is the boundary, not a set
of rules you are being trusted to keep, so inside it you have real tool
access and should use it. Two consequences are worth knowing before they
cost you a turn:

- **Outbound network is an allowlist and fails closed.** A domain that is
  not allowed is a refused connection, not a slow one. Retrying it will not
  help; say what you needed and why instead.
- **Only the workspace survives.** The workspace is a checkout synced back
  to the host. A file written anywhere else is discarded when the sandbox
  is torn down, however carefully you wrote it.
"""

_TAILS: dict[Role, str] = {
    "planner": """\
You plan; you do not build. What you produce is read by a later stage that
has no access to your reasoning, so a plan that is clear to you and
ambiguous to a stranger is a failed plan. Bound the work to the ask: scope
beyond it is a defect, not a bonus.
""",
    "builder": """\
You are the stage that actually does the work: you create and edit files,
run commands, and verify as you go. Your brief names the checkout you work
in and the toolchains resolved for it. Stay inside the ask's scope: a change
beyond it is a defect, and the stage that reviews you is told to treat it as
one.
""",
    "critic": """\
You are a critic, and this session is read-only: you inspect, you judge, and
you never modify what you are reviewing. Your tools are restricted to match,
so a call that is refused is the barrier working, not a fault to route
around. If you lost a capability you needed, say so in your verdict — a
clean verdict you could not actually substantiate is worse than an honest
"I could not check this".
""",
    "operator": """\
You get a piece of work done — research, data handling, calling services,
producing documents, whatever the outcome needs — and you report what you
actually did. The result is not code and is not a pull request unless the
plan asked for one; it is delivered to wherever the run's brief says.
""",
    "concierge": """\
You answer people in chat and drive the loop on their behalf through the
tools you have been given. You have no editor and no shell here: every
capability you have is one of those tools, so when you cannot do a thing,
say which tool you would have needed rather than describing a workaround
the person cannot run. You are talking to a human, so be brief and concrete.
""",
}

# Only the planner. `build.md` and `review.md` already frame the branch and
# the pull request concretely — with the checkout path and the PR number —
# and saying it twice in one session buys nothing. The planner had no such
# framing at all: it was writing task graphs for work it was never told a
# person would read as a diff.
_LANDS_AS_PR = """\
Work that passes the loop's gate is delivered as a pull request a person
reviews, so plan the change the way the repository would want it made, not
the shortest route to a passing check.
"""


def harness_context(config: Config, *, role: Role) -> str:
    """The situation briefing for a session running as ``role``.

    Pure and side-effect free (the config is read, never touched), so the
    exact text every persona receives is unit-testable without a sandbox,
    a worker or a model.
    """
    parts = [_HEAD, _TAILS[role]]
    if role == "planner" and config.github.repo is not None:
        parts.append(_LANDS_AS_PR)
    return "\n".join(part.rstrip() for part in parts) + "\n"


def brief_for_phase(config: Config, phase: str, extra: str | None) -> str:
    """The whole system message one phase's session is opened with: the
    situation briefing for that phase's role, then whatever the phase adds
    of its own (the workload personas in ``phases`` are the callers that
    add anything).

    ``phase`` is a prompt name; an unmapped one is a programming error and
    is reported as such rather than silently drawing the builder's tail.
    """
    try:
        role = ROLE_BY_PHASE[phase]
    except KeyError:
        known = ", ".join(sorted(ROLE_BY_PHASE))
        raise ValueError(f"no harness role for phase {phase!r} (known: {known})") from None
    head = harness_context(config, role=role)
    tail = (extra or "").strip()
    return f"{head}\n{tail}\n" if tail else head
