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

## Environment facts to plan around

Debian/Ubuntu VM; the system Python is externally managed (PEP 668), so
Python dependencies belong in a project virtualenv (`python3 -m venv .venv`)
and commands — including your `verify_commands` — should use `.venv/bin/...`
paths; missing apt packages can be installed with passwordless sudo; network
egress is allowlisted (PyPI, GitHub, and apt mirrors are reachable — declare
anything else in `egress`).

## Response format

Respond with exactly one fenced JSON block:

```json
{
  "steps": ["specific action 1", "specific action 2"],
  "expected_artifacts": ["files or outputs this task should produce"],
  "verify_commands": ["shell commands that exit 0 only when the work is correct"],
  "egress": [{"domain": "registry.npmjs.org", "reason": "npm install for the build"}]
}
```

Steps must be specific enough that an executor with no other context can
follow them. Include the task's own verification ideas in `verify_commands`.

`egress` declares external domains the executor will need to reach beyond
the baseline. PyPI, GitHub, and apt mirrors are always reachable — never
declare those. Each entry needs a short justification; use `[]` when the
baseline suffices (the common case). Domains only — no scheme, path, or
port; `*.example.com` wildcards are accepted. Declarations are auto-granted
only within an operator-set allowlist: a request outside it fails this
plan's validation, so prefer baseline-reachable alternatives.

Respond with ONLY the fenced JSON block — no prose before or after it.
$retry_context
