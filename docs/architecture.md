# sdxloop architecture

sdxloop orchestrates agentic loops on top of [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/)
(the `sbx` CLI). This document describes the system layers, the security
model, and the run lifecycle.

## Layers

```
┌───────────────────────────────────────────────────────────────────┐
│ CLI (typer + rich)         sdxloop run/resume/status/logs/doctor  │
├───────────────────────────────────────────────────────────────────┤
│ Engine                     LoopEngine + PhaseRunner + StateStore  │
│                            (state machine, budgets, checkpoints)  │
├──────────────────────────┬────────────────────────────────────────┤
│ Worker transport         │ GitHub ops facade                      │
│ WorkerClient             │ GithubOps + GithubReporterHook         │
│ (stream / poll)          │ (typed github.op jobs)                 │
├──────────────────────────┴────────────────────────────────────────┤
│ Sandbox layer              SbxCLI → Sandbox → Provisioner → Pair  │
├───────────────────────────────────────────────────────────────────┤
│ Docker Sandboxes (sbx)     microVMs, network policy, secret proxy │
└───────────────────────────────────────────────────────────────────┘

            inside each sandbox: sdxloop-worker
            (JobRunner + agent backends + githubops executor)
```

Two distributions ship from this repo in lockstep versions:

- **`sdxloop`** — everything above the line: the host orchestrator.
- **`sdxloop-worker`** — the in-sandbox runtime. The host package embeds the
  worker wheel (`sdxloop/_vendor/`) at build time so sandboxes can be
  provisioned with no dependency on PyPI availability of sdxloop itself.
  `github-copilot-sdk` sits behind the worker's `[copilot]` extra, so the
  host never installs the Copilot runtime.

## The security primitive: one run = two sandboxes

Every run provisions a **pair** of microVM sandboxes via `Provisioner.ensure_pair`:

| | agent sandbox | github sandbox |
|---|---|---|
| name | `sdxloop-<run>-agent` | `sdxloop-<run>-github` |
| credential | `COPILOT_GITHUB_TOKEN` only | `GH_TOKEN` only |
| injection | `sbx secret set-custom`, bound to `api.githubcopilot.com` + `api.github.com` | built-in `github` secret service |
| network | balanced policy + copilot hosts | balanced policy + github hosts |
| runs | Copilot SDK agent sessions, shell checks | `github.op` jobs (gh CLI or REST) |

Under the default `proxy` secret strategy, **token values never enter either
VM**: sbx stores them in the host keychain and its egress proxy substitutes
them only on requests to the declared hosts. The agent layer can therefore
never exfiltrate the user PAT (it never sees any PAT but its own sentinel),
and the GitHub layer can never spend Copilot quota.

Both sandboxes run under sbx's **balanced** network policy (default-deny plus
a curated allowlist), with per-sandbox allow rules added for exactly the
hosts each role needs. `--app-name sdxloop` isolates all sdxloop state from
the user's interactive sbx usage. The `plain-env` fallback strategy (tokens
written to `~/.sdxloop/env.sh` in-VM) exists for hosts where the experimental
`set-custom` proxying is unavailable, and is documented as weaker.

Cleanup is guaranteed by `SandboxPair` (context manager) plus a process-wide
registry hooked into `atexit` and SIGINT/SIGTERM: aborted runs do not leak
microVMs. Sandboxes are **cattle** — `resume` always provisions a fresh pair.

## The loop

```
outcome ─▶ DECOMPOSE (task DAG) ─▶ per task, dependency order:
             PLAN ─▶ EXECUTE ─▶ SCRUTINIZE ─▶ VERIFY ─▶ VALIDATE ─▶ done
                       ▲            │revise            │fail        │reject
                       └────────────┴──────────────────┘            ▼
                       PLAN ◀── (plan cleared, revisions reset) ─ replan
```

- **DECOMPOSE** — one agent session turns the outcome into a validated task
  DAG (unique ids, resolvable deps, acyclic; `max_tasks` budget).
- **PLAN** — fresh session produces steps, expected artifacts, and
  verify commands for one task.
- **EXECUTE** — fresh session with full tool access does the work in the
  run workspace.
- **SCRUTINIZE** — a fresh session in the *same sandbox* with **read-only
  permissions** reviews the work against plan + acceptance criteria, with
  evidence gathered mechanically (`git status`, `git diff`). Fresh session:
  no anchoring to the executor's claims. Read-only: the critic cannot "fix"
  things. Same sandbox: the workspace under review is preserved.
- **VERIFY** — mechanical: the union of task and plan `verify_commands` must
  all exit 0. No LLM.
- **VALIDATE** — fresh read-only session judges each acceptance criterion.

Failures loop with budgets: scrutiny revisions and verify failures re-EXECUTE
with feedback (`max_revisions_per_task`); validation rejections re-PLAN with
the plan cleared (`max_replans_per_task`). Exhaustion fails the task, skips
its dependents, and finishes the run `failed`. A wall-clock budget bounds the
whole run.

Structured JSON phases are validated against pydantic models with one retry
that feeds the validation error back to the agent.

## Persistence and resume

`StateStore` is a WAL-mode SQLite database at `<state_dir>/state.db` with
four tables: `runs`, `tasks`, `phase_attempts`, `events`. A row is committed
after **every** state transition. Infrastructure failures propagate after
persisting — a crash and a `kill -9` look identical to the store — and
`resume`:

1. re-provisions a fresh sandbox pair,
2. reloads task records (plans included),
3. continues from the last committed transition. A phase whose result was
   never committed re-runs from its start; nothing is replayed.

## Events

Everything observable is an `Event` (versioned JSONL envelope, shared model
in `sdxloop_worker.protocol`). Workers emit `worker.*`, `agent.*`, `gh.*`;
the host adds `run.*`, `task.*`, `phase.*`, `sandbox.*`. All events flow
through the host `EventBus` (synchronous, subscriber-exception-isolated) and
are persisted to SQLite — the CLI TUI, `logs --follow`, and hooks like
`GithubReporterHook` are all just bus subscribers.

See [worker-protocol.md](worker-protocol.md) for the host↔worker contract.
