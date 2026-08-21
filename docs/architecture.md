# sbxloop architecture

sbxloop orchestrates agentic loops on top of [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/)
(the `sbx` CLI). This document describes the system layers, the security
model, and the run lifecycle.

## Layers

```
┌───────────────────────────────────────────────────────────────────┐
│ CLI (typer + rich)         sbxloop run/resume/status/logs/doctor  │
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

            inside each sandbox: sbxloop-worker
            (JobRunner + agent backends + githubops executor)
```

Two distributions ship from this repo in lockstep versions:

- **`sbxloop`** — everything above the line: the host orchestrator.
- **`sbxloop-worker`** — the in-sandbox runtime. The host package embeds the
  worker wheel (`sbxloop/_vendor/`) at build time so sandboxes can be
  provisioned with no dependency on PyPI availability of sbxloop itself.
  `github-copilot-sdk` sits behind the worker's `[copilot]` extra, so the
  host never installs the Copilot runtime.

## The security primitive: one run = two sandboxes

Every run provisions a **pair** of microVM sandboxes via `Provisioner.ensure_pair`.
The github sandbox exists only when the GitHub integration is configured
(`[github] repo = "owner/repo"`); without it, `pair.github` is `None`, `GH_TOKEN`
is not required, and the run has no GitHub capability at all:

|            | agent sandbox                                                                                                                                                           | github sandbox                    |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| name       | `sbxloop-<run>-agent`                                                                                                                                                   | `sbxloop-<run>-github`            |
| credential | `COPILOT_GITHUB_TOKEN` only                                                                                                                                             | `GH_TOKEN` only                   |
| injection  | `sbx secret set-custom`, bound to `api.github.com` (PAT→Copilot token exchange; the exchanged token lives in SDK memory, so copilot API hosts need only network allows) | built-in `github` secret service  |
| network    | balanced policy + copilot hosts + plan-declared grants                                                                                                                  | balanced policy + github hosts    |
| runs       | Copilot SDK agent sessions, shell checks                                                                                                                                | `github.op` jobs (gh CLI or REST) |

Under the default `proxy` secret strategy, sbxloop first attempts sbx's
keychain-backed injection, where **token values never enter the VM**.
Field reality (sbx 0.35): that injection feeds only the interactive agent
sessions sbx launches — never `sbx exec` processes — so provisioning
verifies visibility and **auto-falls-back to an in-VM env file**
(`~/.sbxloop/env.sh`, chmod 600) when the env is invisible, emitting a
`sandbox.secret_env_fallback` event. The fallback fires only on a clean
probe answer: an sbx-level failure during the probe is retried once and
then fails provisioning loudly (`sandbox.secret_probe_error`) — an infra
blip must never silently select the weaker strategy. In fallback mode the token value is
visible inside its own microVM, but the credential *split* still holds
(each sandbox only ever receives its own token) and egress remains bounded
by the balanced network policy.

Both sandboxes run under sbx's **balanced** network policy (default-deny plus
a curated allowlist), with per-sandbox allow rules added for exactly the
hosts each role needs. By default sbxloop shares the user's normal sbx
application state (so `sbx login` and `sbx policy init balanced` apply
directly); setting `app_name` in config opts into isolated sbx state, which
then needs its own `sbx --app-name <name> login` and policy init. The `plain-env` fallback strategy (tokens
written to `~/.sbxloop/env.sh` in-VM) exists for hosts where the experimental
`set-custom` proxying is unavailable, and is documented as weaker.

Beyond the static baseline, egress is **plan-declared and grant-late**: the
PLAN phase may declare extra domains a task needs during EXECUTE (each with a
justification), validated against operator bounds (`[policy] allow` /
`[policy] deny` in sbxloop.toml — out-of-bounds requests fail plan
validation) and applied via `sbx policy allow network <domain> --sandbox <agent>` only at EXECUTE entry. Every grant and refusal is emitted as a
`policy.allow` / `policy.deny` run event, so the persisted event log doubles
as an egress audit trail (`sbxloop logs RUN --type policy.`); `sbxloop config policy` renders the effective per-phase policy. sbx 0.35 has no
revocation primitive, so grants persist for the sandbox's lifetime
(SCRUTINIZE/VERIFY inherit them) but never outlive a run — sandboxes are
removed at run end and `resume` provisions fresh ones.

Cleanup is guaranteed by `SandboxPair` (context manager) plus a process-wide
registry hooked into `atexit` and SIGINT/SIGTERM; signal-triggered teardown
first runs a driver-set quiesce callback (the TUI signals the engine's
cancel flag and briefly joins its thread) so cleanup never races an engine
mid-`sbx exec`. Aborted runs do not leak microVMs: the CLI's first Ctrl+C
removes the run's sandboxes and exits 130 with a `sbxloop resume` hint
(interrupted runs stay resumable), a second force-quits and defers to
`sbxloop sandbox prune`. Sandboxes are **cattle** — `resume` always
provisions a fresh pair.

### The daemon's own sandboxes

`sbxloop daemon` owns two long-lived sandboxes outside any run's pair, both
named per state dir (`sbxloop-daemon-github-<digest>`,
`sbxloop-concierge-<digest>`) and both reported-but-never-pruned by
`sandbox prune`:

- the **github-ops box** (`daemon/github.py`) — polling and issue lifecycle
  with `GH_TOKEN`, provisioned lazily, dropped and re-provisioned on
  failure at most once per five minutes, removed at daemon start/stop;
- the **concierge box** (`daemon/agentbox.py`) — the control channel's
  agent (`daemon/concierge.py`), a Copilot session with the agent token
  and **no built-in tools**: everything it can do is a *host tool*
  (`JobRequest.host_tools`) — daemon control through `control.dispatch`,
  run/item lookups over the stores, `InboxSource.enqueue`, GitHub reads and
  issue triage (file, list, comment, label for a run, close) through the ops
  box — relayed as `agent.tool_request` events and answered
  by the host's `HostToolBroker` with a response file (`sbx cp`) the worker
  polls for. It is **kept across daemon restarts** when the installed worker
  still matches the host, because the SDK session store — the conversation's
  memory, resumed via `resume_session_id` — lives inside the VM.

The credential split holds for both: the host never holds a PAT in a
process that also talks to a model, and neither box holds both tokens.

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

### Prompt templates

Each phase's prompt lives in `packages/sbxloop/src/sbxloop/engine/prompts/*.md`
and is rendered by `sbxloop.engine.prompts.render` as a Python
`string.Template`: `$name` placeholders are substituted strictly (a missing
variable raises), braces need no escaping so JSON examples are pasted verbatim,
and the registry tiers (`$baseline_registries`, `$declarable_registries`) are
injected from `policy.py` rather than written into the files. The flip side is
that a bare `$` anywhere else — a shell `$PID` in an example — breaks rendering
(a literal dollar is `$$`), and `tests/unit/test_prompts.py` pins further
section-level rules: plan.md's environment opener must stay language-neutral,
every ecosystem's notes must keep their markers, and the response-format section
must come last. Each template opens with an HTML comment stating its own
contract (variables, escaping, which test guards which section); `render` strips
that header before the prompt reaches the model, so it costs no tokens and
cannot be mistaken for instructions.

## Persistence and resume

`StateStore` is a WAL-mode SQLite database at `<state_dir>/state.db` with
four tables: `runs`, `tasks`, `phase_attempts`, `events`. A row is committed
after **every** state transition. Infrastructure failures propagate after
persisting — a crash and a `kill -9` look identical to the store — and
`resume`:

1. rehydrates the config persisted at run creation (tokens still come from
   the current environment; `state_dir` stays the one that located the run;
   the `keep_sandboxes`/`keep_on_failure` debug toggles stay resume-time
   choices) and pins the workspace from the `runs` table — editing config between
   start and resume, or resuming from another directory, cannot silently
   change budgets/toggles or relocate the workspace. Any difference from
   the current on-disk config is reported as a `run.config_drift` event,
2. re-provisions a fresh sandbox pair,
3. reloads task records (plans included),
4. continues from the last committed transition. A phase whose result was
   never committed re-runs from its start; nothing is replayed.

## Events

Everything observable is an `Event` (versioned JSONL envelope, shared model
in `sbxloop_worker.protocol`). Workers emit `worker.*`, `agent.*`, `gh.*`;
the host adds `run.*`, `task.*`, `phase.*`, `sandbox.*`. All events flow
through the host `EventBus` (synchronous, subscriber-exception-isolated) and
are persisted to SQLite — the CLI TUI, `logs --follow`, and hooks like
`GithubReporterHook` are all just bus subscribers.

See [worker-protocol.md](worker-protocol.md) for the host↔worker contract.

## Logging

Events are the run's record; the **log** is the daemon's — what the process
did between and around runs, rendered for an operator reading `journalctl`
(or a log shipper). It is [structlog](https://www.structlog.org/) routed
through the standard library (`sbxloop.log`), so third-party stdlib loggers
(discord.py, httpx) render in the same shape and pytest's `caplog` sees
every record.

`sbxloop daemon` configures the pipeline once from `[daemon] log_level`
(`--log-level`, `SBXLOOP_DAEMON__LOG_LEVEL`; default `INFO`) and
`[daemon] log_format` (`console` key=value for humans and journald, `json`
one object per line for ingestion). Third-party loggers are held at
`WARNING` unless `DEBUG` is requested (then `INFO` — never their own DEBUG
firehose). Other CLI commands log at `WARNING` only.

**The run's events are mirrored into the log** by `sbxloop.daemon.logsink`,
subscribed to every run's bus under the logger `sbxloop.run`
(`journalctl … | grep sbxloop.run`), tiered by event type:

- `WARNING` — the run degraded or something was refused: `worker.error`,
  `sandbox.tooling_warning`, `sandbox.resources_warning`,
  `agent.permission_denied`, `agent.tool_cap`, `run.config_drift`.
- `INFO` — lifecycle: `run.*`, task and phase start/state/end, sandbox
  provisioning, worker job start/end/result, GitHub op start/end, policy
  denials, chat (steering) traffic, gc.
- `DEBUG` — everything else: individual tool calls, agent messages and
  deltas, usage, heartbeats, stdout, resource samples, policy allows.

Each record carries the same summary fields `sbxloop logs` prints
(`summarize_event`), plus `run=` and `job=`.

House style for host code:

- `log = get_logger(__name__)`; log **events, not prose** — a stable dotted
  event name (`subsystem.verb_object`: `run.dispatch`, `github.claimed`,
  `sbx.invoke`) and keyword fields for everything that varies. Never format
  values into the event string, never pass f-strings.
- Levels: DEBUG is per-call chatter (tool calls, sbx invocations, polls that
  found nothing, store transitions); INFO is lifecycle (the `daemon.starting`
  config summary, claims, `run.dispatch`/`run.finished` with duration,
  operator commands); WARNING is degraded-but-continuing (retry, fallback,
  a swallowed error, breaker open); ERROR means a human has to look (a
  crash, `run.delivery_failed`, `run.abandoned`, `breaker.opened`, Discord
  gone for the process lifetime).
- Catching an exception and carrying on? Log it with `exc_info=True`.
- Correlation ids are fields — `run=`, `item=`, `job=`, `task=`,
  `sandbox=`. Inside the run thread `bind_run()` stamps `run`/`item` on
  every record via contextvars (they do not cross threads; bind inside the
  thread).
- Never log a secret: subprocess argv goes through `redacted_argv()`, and the
  `redact_secrets` processor masks any field whose name says it is a
  credential (`token`, `secret`, `password`, `api_key`, …).
- `DaemonLoop._notify(text, event, **fields)` is the seam that both narrates
  to Discord (`text`) and logs a structured record (`event`, `fields`).
