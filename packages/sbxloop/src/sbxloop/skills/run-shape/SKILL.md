---
name: run-shape
description: How one run is structured, what carries between its stages, and what the budgets and terminal states mean.
roles: planner, builder, critic
---

# The shape of a run

Read this when you need to know what happens either side of your own stage:
what the stage before you produced, what the stage after you will do with
your output, or why a budget stopped something.

## The stages

A run walks a fixed sequence. Each stage is a separate session with its own
brief; none of them sees another's transcript.

1. **Plan.** One ask becomes a task graph: an ordered set of tasks, each
   with a title, a description, acceptance criteria and the verify commands
   that will judge it. The planner writes the verify commands; nothing
   later may edit them.
2. **Build.** One task at a time. The builder plans and executes in a single
   session, editing the workspace directly.
3. **Verify.** Mechanical. The task's verify commands run exactly as the
   planner wrote them, under a POSIX shell from the workspace root. No model
   is involved and no opinion is taken: they pass or they do not.
4. **Deliver.** The workspace's changes become a commit and, where the loop
   is configured for it, a pull request.
5. **Review.** One adversarial read-only pass over the assembled diff, which
   can send the run back for a bounded number of fix rounds.

A run that produces something other than code walks plan, execute, judge and
publish instead, and its result goes to a configured destination rather than
onto a branch.

## What actually carries between stages

Three things, and nothing else:

- **The workspace.** A checkout synced back to the host. Edits here are the
  run's real output.
- **Your report.** The text you return at the end of your turn. The next
  stage reads it as the account of what happened. Work you did but did not
  report is invisible to it.
- **Structured output**, when your brief asks for JSON. It is parsed, and a
  reply that does not contain the shape asked for is retried once and then
  fails the stage.

Your session's own scratch, your shell history, and anything you wrote
outside the workspace do not carry. Neither does your reasoning: the next
stage sees your conclusions, never how you reached them.

## Budgets, and what running out of one means

Every run is bounded. The bounds exist because an unattended loop that
cannot stop is worse than one that stops early.

- **Revisions per task.** How many times a task may go back to the builder
  after failing verification. Exhausting it fails the task.
- **Replans.** How many times the plan itself may be rewritten when the
  work turns out not to fit it.
- **Tool calls per phase.** A ceiling on one session's tool calls. Calls past
  it are refused with a nudge to stop investigating and report. Hitting it
  is a signal that you are searching rather than deciding: report what you
  have.
- **Wall clock.** A whole-run deadline covering agent work.

None of these is a target to spend. A stage that finishes early and says so
plainly is worth more than one that fills its budget.

## How a run ends

- **done** — the work landed.
- **completed** — the work finished with nothing to land.
- **blocked** — the run could not proceed and a human is needed. This is a
  legitimate ending, not a failure to avoid at all costs: a run that stops
  and says exactly what it needed is more useful than one that guesses and
  delivers something wrong.
- **failed** — a budget ran out, or a stage could not produce usable output.

If you are heading for `blocked`, the most valuable thing you can do is name
the one specific thing that would unblock it: the domain that was refused,
the credential that was missing, the ambiguity in the ask that you could not
resolve.

## Asking the host for something

Some capabilities are not yours to run: they belong to the host, which
answers them on your behalf. Where your brief lists such tools, calling one
is a round trip out of the sandbox and back, and the answer arrives as the
tool result. They are the only way to reach anything outside your sandbox,
so if a tool exists for what you need, use it rather than improvising a way
around the boundary.
