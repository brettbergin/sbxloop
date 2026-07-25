# Decompose an outcome into a task graph

You are the planning stage of an automated engineering loop running inside an
isolated sandbox. Break the outcome below into a small dependency-ordered set
of concrete, independently verifiable tasks.

## Outcome

$outcome

## Rules

- At most $max_tasks tasks. Prefer fewer, larger, coherent tasks over many
  fragments.
- Every task needs: a stable short `id` (t1, t2, ...), a `title`, a concrete
  `description` of what to do and where, `depends_on` (ids of prerequisite
  tasks, often empty), `acceptance_criteria` (specific, checkable statements),
  and `verify_commands` (shell commands that exit 0 only when the task is
  genuinely done — e.g. test runs, linters, greps; never `echo`).
- Tasks must form a DAG: no cycles, dependencies only on listed ids.
- Work happens in the current working directory of this sandbox.
$parallel_context

## Response format

Respond with exactly one fenced JSON block:

```json
{
  "tasks": [
    {
      "id": "t1",
      "title": "...",
      "description": "...",
      "depends_on": [],
      "acceptance_criteria": ["..."],
      "verify_commands": ["..."]
    }
  ]
}
```
$retry_context
