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
│ WorkerClient             │ GithubOps (typed github.op jobs) +     │
│ (stream / poll)          │ engine.landing / engine.review         │
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

Beyond the static baseline, egress is **task-declared and grant-late**: the
DECOMPOSE phase may declare extra domains a task needs during BUILD (each with
a justification), validated against operator bounds (`[policy] allow` /
`[policy] deny` in sbxloop.toml — out-of-bounds requests fail graph
validation) and applied via `sbx policy allow network <domain> --sandbox <agent>` only at BUILD entry. Every grant and refusal is emitted as a
`policy.allow` / `policy.deny` run event, so the persisted event log doubles
as an egress audit trail (`sbxloop logs RUN --type policy.`); `sbxloop config policy` renders the effective per-phase policy. sbx 0.35 has no
revocation primitive, so grants persist for the sandbox's lifetime
(VERIFY, the gate and the review inherit them) but never outlive a run — sandboxes are
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
  run/item lookups over the stores, GitHub reads and issue triage (file —
  queued the moment it is filed — list, comment, label for a run, close)
  through the ops box, and `daemon_log` over the process's own recent log
  lines — relayed as
  `agent.tool_request` events and answered
  by the host's `HostToolBroker` with a response file (`sbx cp`) the worker
  polls for. It is **kept across daemon restarts** when the installed worker
  still matches the host, because the SDK session store — the conversation's
  memory, resumed via `resume_session_id` — lives inside the VM.
  Its `watch_run` tool registers interest in a run so the asker is
  @mentioned in the control channel when that run finishes; the registry
  is **persisted in `daemon_run_watches` and reloaded at startup** (the
  requester's mentionable Discord id stays in-memory only), so a watch
  survives a daemon restart — unless the run itself already finished
  while the daemon was down, in which case the finish event that would
  have fired the notice has already passed and reload drops the stale
  watch instead of reviving it.
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

One run carries an outcome all the way to a merged pull request:

```
outcome ─▶ DECOMPOSE (task DAG) ─▶ per task, dependency order:
             BUILD ─▶ VERIFY ─▶ done
               ▲        │fail (≤ max_revisions: same session resumes)
               └────────┘
               ▲ (revisions exhausted by verify: fresh session, one replan)

        ─▶ GATE ─▶ DELIVER (draft PR) ─▶ REVIEW ─▶ CI ─▶ LAND ─▶ merged
             ▲                            │request_changes / red / conflict
             └──────── FIX (one task) ◀───┘  (≤ max_review_rounds / max_ci_rounds)
```

- **DECOMPOSE** — one agent session turns the outcome into a validated task
  DAG (unique ids, resolvable deps, acyclic; `max_tasks` budget). It
  authors every task's `verify_commands` — the whole mechanical exam, which
  the builder cannot edit (#94: the agent that does the work must not
  author its own exam) — and any per-task `egress` declarations, both
  checked at JSON acceptance.
- **BUILD** — full tool access, plans and does the work in one session,
  narrating its approach first. The one phase that *continues* rather than
  starting fresh: a revision resumes the previous attempt's session where
  the SDK still has it, and is handed that attempt's report either way, so
  it builds on what was already established instead of re-deriving it. A
  replan (or a chat steer) clears the session — the approach it holds was
  the one thrown away.
- **VERIFY** — mechanical: the task's decomposer-authored `verify_commands`
  must all exit 0. No LLM.
- **GATE** — the project's own gate (`[sandbox] gate_command`, or the
  detected `make check` / `just ci` / `npm run check` / tox / nox) over the
  whole tree, mechanical. The decomposer must put the gate in *some* task's
  exam, but a later task can break what an earlier one proved; this is the
  last check on the tree exactly as it will be delivered. A run with no
  `[github] repo` ends `completed` here, its work in the workspace.
- **DELIVER** — the tree becomes one commit on `sbxloop/<run>` and a draft
  PR (see [Delivery](#delivery)); every later round re-delivers onto the
  same branch, so one run is one PR.
- **REVIEW** — a fresh read-only session reads the PR's whole diff
  adversarially (concurrency, failure ordering, trust-boundary parsing,
  cross-module invariants, scope) and returns a verdict with line-anchored
  findings. The verdict is the run's own and is authoritative; it is also
  posted to the PR for the record (as a `COMMENT` review when GitHub
  refuses the loop's identity as a reviewer of its own PR).
- **FIX** — one seeded task (`fix-N`), built and verified like any other
  under the same revision/replan budgets, whose exam is the union of the
  decomposer's verify commands plus the gate. Then back to GATE. Every
  round first merges the current base into the run's clone
  (`hostgit.merge_from_base`): CI judges GitHub's test merge of the branch
  with its base, so a red check may exist only there, and a real conflict
  becomes markers in the fixer's working tree — delivery overlays files
  onto the current base tree and would otherwise overwrite the
  conflicting hunks with the run's version; the conflicted paths ride in
  the brief. Every
  round sees the earlier rounds' findings and the fixer's per-finding
  `addressed` / `refuted: <why>` list, and the next review is told not to
  re-raise a refuted finding without a rebuttal (`RefutedGuard` sends such
  a verdict back once) — the memory the old loop never had.
- **CI** — poll the delivered head's check runs; red fetches the failing
  jobs' logs into a fix brief. "No check runs yet" is trusted as "no CI"
  only after `ci_settle_s`.
- **LAND** — see below.

Two round budgets bound the fix loop: `[landing] max_review_rounds` for
verdicts that request changes, `max_ci_rounds` for the mechanical failures
(a red gate, red CI, a base conflict, a human requesting changes on the PR).
Past either the run ends `failed` with the PR still a draft — nothing
re-picks it unless a human re-labels the issue. There is no per-task
critic: the former SCRUTINIZE/VALIDATE stages audited task completion and
rubber-stamped it (6/6 pass, 5/5 accept in the measured baseline) while
diff-level defects leaked to the PR; one adversarial pass over the
assembled diff is the critic that earns its turns.

Failures loop with budgets: verify failures re-BUILD with the failure
transcript as feedback (`max_revisions_per_task`); exhausting revisions on
verify failures spends a replan (`max_replans_per_task`) and restarts with a
fresh session. Exhaustion fails the task, skips its dependents, and finishes
the run `failed` before anything is delivered. `[budgets] max_wall_clock_s`
bounds the agent's work; time spent waiting on GitHub (CI, landing) is not
charged to it and is bounded separately by `[landing] ci_timeout_s`.

Structured JSON phases are validated against pydantic models with one retry
that feeds the validation error back to the agent.

Every stage is a run state — `building`, `gating`, `delivering`,
`reviewing`, `fixing`, `awaiting_ci`, `landing` — and the last one entered
is kept in `runs.stage`, so a run that ends `failed` or `blocked` still
knows where a resume re-enters. The sandbox pair stays alive across the
waits (a fix round needs the agent sandbox, a poll the github one; an idle
microVM is cheap); a chat message or a cancel wakes a wait at once rather
than at the next poll interval.

### What a run costs

A run's spend and its wall clock are both governed by **turns**, not jobs.
Every turn re-sends the whole session context, and field measurement (run
`rews3ssdn`: 272 turns across 25 jobs) put ~22k tokens of fixed context on
every one of them, against a phase prompt under 2k. Wall clock tracked the
same turn count at ~10s/turn.

That fixed context is, however, overwhelmingly **cached**, not re-billed:
run `rrhb28j7n` shows `cache_read_tokens` is a subset of `input_tokens` —
turn 0 writes ~20k and turn 1 reads exactly that back — and 86.5% of the
run's input tokens (3,615,785 / 4,180,827) are cache reads: executor 91.7%,
planner 83.3%, validator 82.4%, scrutinizer 79.1%, decomposer 68.5%. The
earlier "62% of spend" figure was 62% of *tokens*, and those tokens bill at
cache-read rates. Trimming the static prefix therefore has little to win and
would invalidate the cache; the knob that did it is gone.

The same run also settles what `AssistantUsageData.cost` is *not*: it reports
the same constant (15.0) on every turn of every session, so it is not a
per-turn delta and summing it fabricates a figure. Its unit is unknown — a
constant per turn reads far more like a premium-request multiplier or a quota
unit than a currency amount — so it must not be rendered as currency. The
wire `Usage` model therefore carries **no** spend field at all (#439): no
backend writes one, so keeping the field and its render was dead code one
hand-built object away from reintroducing the fabricated figure. `run_usage`
and `usage_today` end their block with a fixed "spend: not reported by the
agent backend" line, and any future backend figure must arrive with its unit
established and be carried non-additively.

One knob remains:

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
section-level rules: build.md's environment notes and decompose.md's
verify-authoring rules must keep their field-regression markers, and every
ecosystem's notes must keep theirs. Each template opens with an HTML comment stating its own
contract (variables, escaping, which test guards which section); `render` strips
that header before the prompt reaches the model, so it costs no tokens and
cannot be mistaken for instructions.

## Persistence and resume

`StateStore` is a WAL-mode SQLite database at `<state_dir>/state.db` with
four tables: `runs`, `tasks`, `phase_attempts`, `events`. It runs
`synchronous=NORMAL`, which is the safe setting under WAL: commits no longer
fsync one-by-one, and a crash can only lose the tail of the WAL, never
corrupt the database. Streaming `agent.message_delta` events are *not*
persisted — they are per-chunk UI telemetry that live surfaces (TUI,
Discord) read off the bus, while the full `agent.message` carries the same
text and is committed like every other event; resume never reads deltas, so
`sbxloop logs` differs only by those chunk lines. A row is committed
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
3. reloads task records,
4. continues from the last committed transition — the task graph if the run
   never delivered, else the pipeline stage in `runs.stage` (a re-delivery
   is idempotent: the branch is force-moved and the open PR reused; a
   review that never committed its verdict runs again). A phase whose
   result was never committed re-runs from its start; nothing is replayed.

### Run reconciliation

Because the run row is only ever written by the in-process run loop, a dead
process (or a cancelled work item) used to leave runs stuck in `running` or
`decomposing` forever, so `list_runs` and `!sbx status` disagreed about what
was active (#374). Two sweeps keep them honest, and both only ever *append*
chronology (a `run.reconciled` event) — historical events are never mutated:

- **Startup reconciliation** closes every non-terminal run (anything not in
  `merged`/`completed`/`failed`/`blocked`/`cancelled`) that is neither the
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

## Landing

A delivered PR is not done because it exists, and merging is not optional:
`engine/landing.py` drives the PR to a decision, polling in the order of
what each gate costs — the PR's own fate first (a human merging it is the
acceptance; a human closing it unmerged fails the run), then un-drafting,
then a human's standing `CHANGES_REQUESTED` (the loop's own reviews are
excluded from that fold — our verdict lives in the run), then CI, then
mergeability, and only then the merge:

1. **Draft → ready.** REST cannot un-draft a pull request, so this one call is
   GraphQL (`markPullRequestReadyForReview`) through the same `raw.api`
   transport as everything else. GraphQL reports a failed mutation with a 200
   status and an `errors` array, so the body is the verdict, not the status.
   Un-drafting and merging are separate polls on purpose: GitHub reports a
   draft's `mergeable_state` as `draft`, so the PR's real merge state only
   becomes readable once it is out of draft.
2. **Behind → update-branch.** Protection commonly requires a PR to be up to
   date before merging, and the base moves. One API call, not a run — but
   bounded (`[landing] merge_update_attempts`), because a base moving faster
   than CI finishes would update for ever. The head an update was requested
   at is recorded so a later poll can tell an update still in flight (head
   unchanged) from one that has landed, rather than spending the budget
   asking twice; the request carries `expected_head_sha`, so an update
   racing another push fails rather than merging over it.
3. **Conflicted, red, or objected to → a fix round**, on the CI budget. A
   re-delivery rebuilds the commit on the current base, so a real conflict
   is genuinely fixable.
4. **Merge**, sending the head sha the loop actually judged. A push that
   landed since loses the race with a 409 rather than being merged over.

Two answers come back as *data* rather than as exceptions, and the difference
matters: **405** is GitHub's blanket "not mergeable right now" — a protection
rule wanting an approval this identity cannot give, most often — which no
retry fixes, so the run ends `blocked` with the PR left open and out of
draft for a human (and resumes at `landing` once they have acted); **409** is
a race, so the next poll simply re-judges the new head. A draft that will
not clear, CI that never reports within `ci_timeout_s`, and an update budget
spent are `blocked` for the same reason: nothing another round would change.

## The daemon

`sbxloop daemon` is deliberately small: it claims issues carrying
`sbxloop:run` in the one configured repository (a label swap plus a claim
comment as the optimistic lock), runs each as **one** engine run, and
reports the outcome on the issue — closed with `sbxloop:completed` when the
PR merged (the PR body's `Closes #N` closes it even if the daemon is down),
`sbxloop:failed` when the run gave up (after the per-item attempt cap and
its backoff), `sbxloop:blocked` when GitHub would not let the loop finish.
It never files work of its own: only a human labelling an issue — directly,
or by asking the Discord concierge, which files the issue *with* the label —
starts a run. Everything else the daemon does is a guardrail or a
recovery: the calendar-day run cap, the circuit breaker, the resume cap,
pause and cancel, startup and staleness reconciliation, run-directory
retention.

The **run cap** is a wall-clock calendar-day gate: it counts the runs whose
start time falls on the current day in `[daemon] run_cap_timezone` (any
IANA zone, default `UTC`) and compares that against
`[daemon] max_runs_per_day` (default 12). The count resets at 00:00 in that
zone, so a run started at 23:59 still occupies a slot for the remaining
minute and frees it only at the boundary. This gate is distinct from the
per-item attempt cap, the per-item resume cap and the persisted
consecutive-failure circuit breaker, which are unaffected by the day
boundary. Operator strings name both the day and the zone
(`runs today (UTC): 7/10, resets at 00:00 UTC`).

## Events

Everything observable is an `Event` (versioned JSONL envelope, shared model
in `sbxloop_worker.protocol`). Workers emit `worker.*`, `agent.*`, `gh.*`;
the host adds `run.*`, `task.*`, `phase.*`, `sandbox.*`. All events flow
through the host `EventBus` (synchronous, subscriber-exception-isolated) and
are persisted to SQLite — the CLI TUI, `logs --follow`, the daemon's log
sink and the Discord bridge are all just bus subscribers.

The stages that decide something also emit what they *decided*, parsed:
`run.tasks` (the roster, re-announced on resume and after a fix task is
appended), `review.verdict` (the round, the verdict, how many findings and
how many block, the posted review's url), `fix.round` (the round, its kind
— `review`, `gate`, `ci`, `conflict`, `human` — the task appended and the
budget it spent), `ci.status` (the folded check-run state, emitted on change
only), `land.undraft` / `land.update`, and `run.merged` / `run.blocked` with
the PR and why. `run.state` fires on every stage entry — the state *is* the
stage. These carry no information the agent's reply did not — they exist so
a surface can show the decision without showing the agent's JSON, which is
what the Discord bridge does.

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
  in-flight start for that tool, for workers predating `tool_call_id`). A
  start still in flight survives routine flushes — its one line lands on
  completion — and only the run-end `flush(final=True)` renders leftovers as
  `… running`, so a mid-run flush (a failed sibling, the coalesce timer)
  cannot print the same call twice.
- **Bounded output excerpts.** `output_excerpt` gives a completed call a
  header (✓/✗ plus exit status) and a fenced head+tail excerpt of its output,
  with any elision marked `… N lines elided …` counted from the event's
  `output_lines`. A failure gets the larger budget and prefers `error`
  (stderr); a success is quiet by default, because the batched line already
  reports it. The line-selection half of that policy — the budgets, the
  head+tail split, the per-line clip and the elision marker — lives in
  `sbxloop.excerpt` so every renderer shares one copy: the caps are named
  constants there, `TOOL_OUTPUT_LINES_DEFAULT` (0),
  `TOOL_FAIL_OUTPUT_LINES_DEFAULT` (20) and `TOOL_EXCERPT_LINE_CLIP` (300
  chars/line), with the two line budgets configurable as
  `[discord] tool_output_lines` / `tool_fail_output_lines`. Discord's own
  character caps stay in `discord_format`: the fenced body is clipped to
  `TOOL_EXCERPT_MAX_CHARS` (1200) and the finished message to
  `DISCORD_MAX_MESSAGE`, so no input can overflow Discord's limit. The
  `sbxloop watch` TUI renders a failed tool call through the same shared
  helper, so its excerpt has the same head+tail shape, per-line clip and
  elision marker rather than a plain last-N-lines tail.
- **Redaction at the render seam.** The worker already redacts an event's
  output before it leaves the sandbox; because this feature *publishes* more
  of what a command printed, every rendered command and every excerpt passes
  through `sbxloop.log.redact_text` again on the way to a thread. It is
  idempotent, so text already masked upstream is unchanged. The credential
  vocabulary is deliberately spelled twice — `sbxloop_worker.secrets` (worker
  side; the worker cannot import sbxloop) and `sbxloop.log` (host side) — and
  the two word lists differ slightly, so a word added to one should be
  weighed for the other. Both anchor the word to whole `_`/`.`/`-`-delimited
  name segments, so `PATH=`, `--patch`, `compat=1` or a pytest `tokens: 5`
  line are never masked.
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
  pipeline decided (`review.verdict`, `fix.round`, `ci.status`, `land.*`),
  sandbox provisioning, worker job start/end/result, GitHub op start/end,
  policy denials, chat (steering) traffic, gc.
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
