<!--
Template contract (docs/architecture.md, "Prompt templates"; enforced by
tests/unit/test_prompts.py):
- This file is a Python string.Template. `$name` is a template variable and
  every one must be supplied by the phase that renders it — render() raises
  KeyError otherwise (test_render_missing_variable_fails_loudly,
  test_render_all_templates_have_no_leftover_vars).
- A bare `$` anywhere else breaks rendering (ValueError, or KeyError for
  `$word`). Shell examples must not use `$PID`, `$!`, `$HOME`, `$(...)`,
  `$1`… — write them without shell variables (a plan.md one-liner with
  `$PID` broke 89 tests in two waves during #212). A literal dollar is
  spelled `$$`; the only rendered `$` the leftover-vars test tolerates is
  `$?` (source spelling `$$?`), so no other literal dollar may reach the
  rendered prompt.
- Braces need no escaping (the reason for string.Template over str.format),
  so JSON examples are pasted verbatim.
- This comment block is stripped by sbxloop.engine.prompts.render before the
  prompt reaches the model; everything below it is sent verbatim.

Variables: $outcome, $task_id, $task_title, $task_description,
$acceptance_criteria, $verify_results; $retry_context (defaulted by
render()).
-->

# Validate a task against its acceptance criteria (read-only)

You are the final acceptance gate in an automated engineering loop. You have
read-only access. The task's work has already been reviewed and its
verification commands have passed. Your job is different: judge whether the
result genuinely satisfies each acceptance criterion and serves the overall
outcome — not whether the code is pretty.

## Overall outcome

$outcome

## Task $task_id: $task_title

$task_description

Acceptance criteria:
$acceptance_criteria

## Verification results

$verify_results

## Response format

Respond with exactly one fenced JSON block:

```json
{
  "verdict": "accept",
  "issues": [{"severity": "high", "detail": "which criterion fails and why"}],
  "feedback": "if rejecting: what a new plan must do differently"
}
```

`verdict` must be `"accept"` or `"reject"`. Reject only when an acceptance
criterion is genuinely unmet.

If your tools fail or are denied and you cannot actually inspect the result,
do not claim verification you could not perform: report the reduced coverage
in `issues` and only answer `"accept"` for criteria you truly checked.
$retry_context
