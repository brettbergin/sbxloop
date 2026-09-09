---
name: verify-gate
description: How verification and the project gate actually run, and how to read a red one.
roles: builder, critic
---

# Verification and the gate

Read this when a check has gone red and you need to know what it was
actually asserting, or when you are deciding whether work is finished.

## Two different things

**Verify commands** belong to one task. The planner wrote them; you cannot
edit them, and they run exactly as written. They are that task's exam.

**The project gate** is the repository's own whole-tree check — whatever the
project itself runs before it accepts a change. It runs once over everything
before delivery, not per task. A task can pass its own verify commands and
still be stopped by the gate, because the gate sees the tree the other tasks
also touched.

## How verify commands run

Under a POSIX shell, from the **workspace root**, with no interactive
terminal. Three consequences that repeatedly cost a revision:

- **Paths are root-relative unless the command says otherwise.** If a
  command names a path, put the file at that path. Do not bury a project one
  directory deeper than the command looks; do not move a file the command
  already finds. If a command enters a subdirectory, build there.
- **No shell you did not ask for.** No profile is sourced, so a tool that
  only works after an activation step must be invoked by its real path or
  set up by the command itself.
- **Exit status is the whole verdict.** Output is captured for a human and
  for the next stage, but pass or fail is the exit code.

## Reading a red result

Work the failure back to its cause before changing anything.

1. **Read the first failure, not the summary.** The summary counts; the
   first failure diagnoses. Output is clipped head and tail for exactly this
   reason, so the top of what you were shown is the useful part.
2. **Decide which of three things it is.** The work is wrong; the work is
   fine but is not where the command looks; or the command cannot pass as
   written. These need completely different responses, and guessing between
   them is what burns a budget.
3. **Change one thing.** A red check that stays red after a change you
   cannot connect to it means you have not found the cause yet.

If the identical command fails identically twice, stop repeating the
attempt. Either the work must be made to satisfy the command as written -- a
different layout, a path it actually inspects, setup it needs -- or the
command genuinely cannot pass, in which case say so plainly in your report
with the evidence. A human can act on a clear diagnosis; nobody can act on a
third silent retry.

## Judging a change against a gate

Compare against what the base branch **already requires and already has**,
never against "everything green". A check that was already failing on the
base is not this change's fault, and demanding it be fixed here is scope
creep. A check that this change turned red is this change's problem, whether
or not it looks related.

The same applies to coverage, lint counts and warnings: the question is
always "did this change make it worse", not "is it perfect".

## When you cannot run the check

Say so, and say why. A verdict of "passes" that rests on a check you could
not actually execute is worse than no verdict, because the stage after you
has no way to tell the difference. Name the command you could not run and
what stopped it.
