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
  box, and `daemon_log` over the process's own recent log lines — relayed as
  `agent.tool_request` events and answered
  by the host's `HostToolBroker` with a response file (`sbx cp`) the worker
  polls for. It is **kept across daemon restarts** when the installed worker
  still matches the host, because the SDK session store — the conversation's
  memory, resumed via `resume_session_id` — lives inside the VM.
  Its `watch_run` tool registers interest in a run so the asker is
  @mentioned in the control channel when that run finishes; the registry
  (and the requester's mentionable Discord id) lives in the Discord
  bridge's memory only, so **a daemon restart forgets every watch**.
  `daemon_log` is served from a ring-buffer handler `configure_logging`
  installs in `sbxloop/log.py` — a `deque` with a `maxlen`, so a long-lived
  daemon's memory is bounded; the append is one atomic deque operation with
  no locks or I/O, keeping it off the hot path, and it stores the line the
  stderr handler already rendered and redacted.

The credential split holds for both: the host never holds a PAT in a
process that also talks to a model, and neither box holds both tokens.

One deliberate exception to "the host does not talk to the network": the
version check (`daemon/versions.py`, the concierge's `version_status` tool
and the startup drift notice) reads `pypi.org` from the host process. It is
unauthenticated and carries no credential, so the split above is untouched;
it is bounded by a short timeout, a response cap and a five-minute memo, and
every failure degrades to "could not reach PyPI" rather than raising.

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
- **EXECUTE** — full tool access, does the work in the run workspace. The
  one phase that *continues* rather than starting fresh: a revision resumes
  the previous attempt's session where the SDK still has it, and is handed
  that attempt's report either way, so it builds on what was already
  established instead of re-deriving it. A replan clears the session — the
  approach it holds was the one thrown away.
- **SCRUTINIZE** — a fresh session in the *same sandbox* with **read-only
  permissions** reviews the work against plan + acceptance criteria, with
  evidence gathered mechanically (`git status`, `git diff HEAD`). Fresh
  session: no anchoring to the executor's claims. Read-only: the critic
  cannot "fix" things. Same sandbox: the workspace under review is
  preserved. The diff is handed over in full (clipped head+tail at
  `DIFF_CLIP`) rather than as a `--stat` summary: a critic that has to
  rediscover the change by opening files spends turns, and turns are what a
  run is billed and timed by.
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

### What a run costs

A run's spend and its wall clock are both governed by **turns**, not jobs.
Every turn re-sends the whole session context, and field measurement (run
`rews3ssdn`: 272 turns across 25 jobs) put ~22k tokens of fixed context on
every one of them — roughly 62% of the run's input spend, against a phase
prompt under 2k. Wall clock tracked the same count at ~10s/turn. Two knobs
follow from that:

- `[budgets] trim_system_message` (default **off**) drops the agent SDK's
  system-message sections a phase cannot act on (`PHASE_DROP_SECTIONS` in
  `sbxloop.engine.phases`), so they are not re-sent every turn. Only
  `code_change_rules`, and only from the phases that write no code. Off until
  a real run says whether that 22k is billed or cached, and whether the SDK
  accepts the `customize` config shape — a rejection fails every agent job,
  and the deploy health check would not catch it because it starts no run.
- `[budgets] max_parallel_tasks` runs independent tasks concurrently. The
  task DAG already knows which tasks are independent; above 1 they share one
  agent sandbox and one workspace, so raising it is safe only for outcomes
  whose tasks are genuinely file-disjoint — `depends_on` is agent-authored
  and does not certify that.

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

### Run reconciliation

Because the run row is only ever written by the in-process run loop, a dead
process (or a cancelled work item) used to leave runs stuck in `running` or
`decomposing` forever, so `list_runs` and `!sbx status` disagreed about what
was active (#374). Two sweeps keep them honest, and both only ever *append*
chronology (a `run.reconciled` event) — historical events are never mutated:

- **Startup reconciliation** closes every non-terminal run (`created`,
  `provisioning`, `decomposing`, `running`, `finalizing`) that is neither the
  run executing in this process nor one pinned for resume.
- **The staleness safety net** runs every tick — including while paused — and
  closes any non-terminal run whose last activity (chronology, falling back to
  the run row's `updated_at`) is older than `[daemon] run_stale_after_s` while
  no run is executing. The default is 6h (`21600`); `0` disables this sweep.

The reason recorded depends on the associated work item: a `cancelled` item
gives run state `cancelled` with reason `work item cancelled` (plus the
operator attribution from the item's last error), anything else gives `failed`
with `orphaned: daemon restarted while run was in flight` (or, from the
staleness sweep, `orphaned: stale, no activity for <n>s`). Cancellation itself
transitions the run record alongside the work item, so the two cannot diverge.

The run that is legitimately in flight is **never** reconciled: both sweeps
skip the daemon's `current` run and any item queued for resume, and the
staleness sweep does not run at all while a run is executing. The persisted
reason is surfaced next to the state in `sbxloop status` / `list_runs` output,
on the `reason:` line of `sbxloop status <run>`, and in the TUI run header.

## Daemon guardrails

The daemon's **run cap** is a wall-clock calendar-day gate: it counts the runs whose start time falls on the current day in
`[daemon] run_cap_timezone` (any IANA zone, default `UTC`) and compares that
against `[daemon] max_runs_per_day` (default 12, key name unchanged). The
count resets at 00:00 in that zone, so a run started at 23:59 still occupies
a slot for the remaining minute and frees it only at the boundary; the same
day window backs the per-day review and post-mortem caps. This gate is
distinct from the per-item attempt cap, the per-item resume cap and the
persisted consecutive-failure circuit breaker, which are unaffected by the
day boundary. Operator strings name both the day and the zone
(`runs today (UTC): 7/10, resets at 00:00 UTC`).

## Events

Everything observable is an `Event` (versioned JSONL envelope, shared model
in `sbxloop_worker.protocol`). Workers emit `worker.*`, `agent.*`, `gh.*`;
the host adds `run.*`, `task.*`, `phase.*`, `sandbox.*`. All events flow
through the host `EventBus` (synchronous, subscriber-exception-isolated) and
are persisted to SQLite — the CLI TUI, `logs --follow`, and hooks like
`GithubReporterHook` are all just bus subscribers.

The phases that ask their agent for JSON (decompose, plan, scrutinize,
validate) also emit what they *decided*, parsed: `run.tasks` (the roster,
re-announced on resume with each task's persisted state), `phase.plan` (the
steps, expected artifacts, verify commands and egress grants a task will be
executed against) and `phase.verdict` (a critic's call, its issues and the
feedback the executor is about to be told). These carry no information the
agent's reply did not — they exist so a surface can show the decision without
showing the agent's JSON, which is what the Discord bridge does.

See [worker-protocol.md](worker-protocol.md) for the host↔worker contract.

### Tool calls in a run thread

A watcher reads a run thread to see what the agents are *executing*, so tool
calls get their own rendering rules (`sbxloop.cli.cmdfmt`,
`sbxloop.daemon.discord_format`):

- **Informative truncation.** A command is rendered by
  `cmdfmt.format_command`: whitespace collapses, the boilerplate
  `cd <absolute run path> &&` prefix — identical on every call in a run, and
  the thing naive middle-elision spends the whole budget on — collapses to
  `cd $RUN &&`, and if the line is still over `COMMAND_DISPLAY_CLIP` (160
  characters, a named default a caller may override) the *longest argument
  tokens* are elided one at a time. The leading verb therefore always
  survives, and any token that lost characters carries a literal `…`, so a
  token in the output is never a silently truncated one. This is display-only:
  the stored event keeps the full command, so `run_events`, `sbxloop logs` and
  resume are unaffected.
- **One entry per call.** `ToolBatcher` records `agent.tool_start` as pending
  and emits nothing; the single line is written when `agent.tool_end` arrives:
  `$ bash  cd $RUN && uv run mypy  ✓ 1.5s` or `✗ exit 1 · 1.5s`. Correlation is
  by `tool_call_id`, never by comparing command text, so parallel calls
  completing out of order still carry their own command. An end with no
  matching start renders from its own `args` (falling back to the oldest
  in-flight start for that tool, for workers predating `tool_call_id`), and a
  start still in flight at flush is shown as `… running`.
- **Bounded output excerpts.** `output_excerpt` gives a completed call a
  header (✓/✗ plus exit status) and a fenced head+tail excerpt of its output,
  with any elision marked `… N lines elided …` counted from the event's
  `output_lines`. A failure gets the larger budget and prefers `error`
  (stderr); a success is quiet by default, because the batched line already
  reports it. The caps are named constants — `TOOL_OUTPUT_LINES_DEFAULT` (0),
  `TOOL_FAIL_OUTPUT_LINES_DEFAULT` (20), `TOOL_EXCERPT_LINE_CLIP` (300
  chars/line), `TOOL_EXCERPT_MAX_CHARS` (1200) — with the two line budgets
  configurable as `[discord] tool_output_lines` / `tool_fail_output_lines`.
  The finished message is additionally clamped to `DISCORD_MAX_MESSAGE`, so no
  input can overflow Discord's limit.
- **Redaction at the render seam.** The worker already redacts an event's
  output before it leaves the sandbox; because this feature *publishes* more
  of what a command printed, every rendered command and every excerpt passes
  through `sbxloop.log.redact_text` again on the way to a thread. It is
  idempotent, so text already masked upstream is unchanged.
- **Additive schema.** `tool_call_id`, `output_lines` and `duration_ms` on
  `agent.tool_end` are optional; nothing was removed or renamed. Consumers
  tolerate their absence, so an older worker's chronology renders (without
  duration or elision counts) and no migration of existing chronologies is
  required.

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
- `INFO` — lifecycle: `run.*`, task and phase start/state/end, what the
  structured phases decided (`phase.plan`, `phase.verdict`), sandbox
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
