<!--
Contract for this template. Rendered with string.Template ($-substitution),
so a literal dollar is spelled `$$`; the only rendered `$` the leftover-vars
test tolerates is `$?` (source spelling `$$?`). Shell examples here must use
no shell variables at all.

Variables: $task_title, $task_description, $acceptance_criteria,
$suspect_command, $suspect_output, $other_commands, $builder_report,
$gate_rule, $retry_context

Braces need no escaping, so the JSON example is pasted verbatim. This
comment block is stripped by sbxloop.engine.prompts.render before the prompt
reaches the model; everything below it is sent verbatim.
-->

# Re-author one verification check

A task's work is finished and every check on it passes but one. That one has
now failed with identical output across more than one attempt, against more
than one approach to the work, so nothing further done to the work can
change its result. Either it asks for something this environment cannot
give, or it asks for the wrong thing.

You decide what happens to that one check. You may not change the work, and
you may not touch any other check.

Be strict. A check exists to catch work that is wrong, and the person who
reads the resulting pull request is trusting these checks to have run. It is
better to leave a check standing and let the task fail than to weaken the
exam so the work can pass.

## The task

$task_title

$task_description

Acceptance criteria:
$acceptance_criteria

## The check that keeps failing

```
$suspect_command
```

Its output, the same on every attempt:

```
$suspect_output
```

## The other checks on this task

These stay exactly as they are, whatever you decide. Read them: a property
one of them already covers does not need covering again.

$other_commands

## What the builder reported

$builder_report

## Your options

- **replace** — the property this check tests is worth testing and can be
  tested here, but not the way this command tests it. Give a command that
  tests the same property and can actually pass.
- **drop** — the property cannot be tested in this environment at all, by
  any command. Something needing a running server, a rendering engine, a
  real browser, a device, a deployed address, or a human's eyes is not
  testable here. The check is removed and its removal is reported, so the
  reviewer knows to check that property by hand.
- **keep** — the check is right and can pass; the work has genuinely not
  satisfied it. The task fails and a human reads the diagnosis.

Prefer **replace** over **drop** whenever any part of the property survives
as something a command can decide. A build artifact's contents, a file's
presence or shape, a type check, a lint rule, the project's own test suite:
these reach much of what people reach for a running system to observe.

## What a replacement must be

- It decides and then exits on its own. It never starts anything
  long-running, never waits on a server it started, and never signals a
  process by name or pattern.
- It is offline. No network address, no remote API, no deployed URL: a rate
  limit or a flake must never be able to fail work that is done.
- It judges the workspace and what the build produced from it, nothing
  about the machine it runs on.
- It is portable POSIX shell, run from the workspace root, and it names any
  subdirectory it depends on explicitly.
- It can still fail. A command that passes no matter what the workspace
  contains is not a check, and answering with one is worse than **keep**.

$gate_rule

$retry_context

## Answer

Respond with ONLY one fenced json block, no prose before or after it.
`command` is required for **replace** and ignored otherwise. `reason` is one
sentence, and for **drop** it must name the property that is no longer being
tested, because it is copied into the report a human reads.

```json
{
  "verdict": "replace",
  "command": "the replacement check, or empty for drop/keep",
  "reason": "one sentence"
}
```
