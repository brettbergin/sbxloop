# Plan one task

You are the planning stage of an automated engineering loop. Produce a
concrete execution plan for the task below. Do not execute anything yet.

## Overall outcome

$outcome

## Task $task_id: $task_title

$task_description

Acceptance criteria:
$acceptance_criteria

## Prior feedback

$feedback

## Response format

Respond with exactly one fenced JSON block:

```json
{
  "steps": ["specific action 1", "specific action 2"],
  "expected_artifacts": ["files or outputs this task should produce"],
  "verify_commands": ["shell commands that exit 0 only when the work is correct"]
}
```

Steps must be specific enough that an executor with no other context can
follow them. Include the task's own verification ideas in `verify_commands`.

Environment facts to plan around: Debian/Ubuntu VM; the system Python is
externally managed (PEP 668), so Python dependencies belong in a project
virtualenv (`python3 -m venv .venv`) and commands — including your
`verify_commands` — should use `.venv/bin/...` paths; missing apt packages
can be installed with passwordless sudo; network egress is allowlisted
(PyPI, GitHub, and apt mirrors are reachable — other registries may not be).
$retry_context
