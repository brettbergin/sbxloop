# sdxloop

**Agentic loop orchestration on [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`), with hard credential isolation.**

sdxloop turns a large outcome ("migrate this service to async", "add coverage to every untested module") into a supervised agentic loop: it **decomposes** the outcome into a task graph, then for each task **plans → executes → scrutinizes → verifies → validates**, with revision/replan budgets, checkpointing, and resume.

## The primitive: a sandbox pair

Every run gets **two isolated microVM sandboxes**, so no single environment ever holds both credentials:

| Sandbox | Credential | Purpose |
|---|---|---|
| `sdxloop-<run>-agent` | `COPILOT_GITHUB_TOKEN` (fine-grained PAT, *Copilot Requests* permission) | Runs the [GitHub Copilot SDK](https://github.com/github/copilot-sdk) agentic layer. All model calls and tool executions happen inside this VM. |
| `sdxloop-<run>-github` | `GH_TOKEN` (fine-grained PAT: issues write, contents read, …) | Performs user-facing GitHub operations (issues, PRs, statuses) on your behalf. |

Both sandboxes run under sbx's **balanced network policy** (default-deny egress plus a curated allowlist), and tokens are injected through sbx's secret proxy — **credential values never enter the VM**; the host proxy substitutes them only on egress to their declared domains.

## Quickstart

```bash
pip install sdxloop

# one-time host setup
sbx login
sbx policy init balanced
sdxloop doctor          # verifies sbx, policy, tokens, worker wheel

# go
sdxloop run "Add mypy strict typing to every module in ./src and fix all findings"
```

Or as a library:

```python
from sdxloop import LoopEngine, load_config

engine = LoopEngine(config=load_config())
result = engine.start(outcome="Add mypy strict typing to ./src and fix all findings")
print(result.state, result.run_id)
```

## How a run works

```
outcome ──▶ DECOMPOSE (task DAG) ──▶ for each task:
              PLAN ─▶ EXECUTE ─▶ SCRUTINIZE ─▶ VERIFY ─▶ VALIDATE ─▶ done
                        ▲             │revise            │fail        │reject
                        └─────────────┴──────────────────┘            │
                        └── replan ◀──────────────────────────────────┘
```

- **Scrutinize** — a fresh, read-only critic session reviews the diff and artifacts.
- **Verify** — mechanical: the task's `verify_commands` must exit 0. No LLM.
- **Validate** — a fresh read-only session judges the acceptance criteria.
- Budgets bound revisions, replans, tasks, and wall clock. State is checkpointed to
  SQLite after every transition; `sdxloop resume <run>` re-provisions sandboxes
  (they're cattle) and continues where it left off.

## Repository layout

This repo is a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) with two distributions:

- [`packages/sdxloop`](packages/sdxloop) — the host orchestrator: sbx CLI wrapper, sandbox pair provisioning, worker transport, loop engine, typer CLI + rich TUI.
- [`packages/sdxloop-worker`](packages/sdxloop-worker) — the in-sandbox runtime: shared protocol models, job runner, Copilot backend. Installed into sandboxes automatically (the host package embeds the worker wheel, so this works before anything is on PyPI).

## Development

```bash
make install    # uv sync --all-packages
make check      # ruff format --check + ruff check + mypy --strict + pytest --cov
make build      # build both wheels
```

Unit and contract tests run against a **fake sbx CLI** — no Docker Sandboxes install
is required for development. The real-sbx end-to-end suite runs in CI via a manually
dispatched workflow.

## Requirements

- Python ≥ 3.11
- [Docker Sandboxes (`sbx`)](https://docs.docker.com/ai/sandboxes/) on the host (macOS Apple silicon, Windows 11, or Ubuntu 24.04+/KVM)
- A GitHub Copilot subscription (any plan) + two fine-grained PATs (see above)

## License

MIT
