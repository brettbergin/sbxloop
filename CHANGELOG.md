# Changelog

All notable changes to sbxloop are documented here. The project adheres to
[Semantic Versioning](https://semver.org/) and both distributions (`sbxloop`,
`sbxloop-worker`) release in lockstep.

## [Unreleased]

### Changed

- **The plan and execute prompts no longer teach Python as the default
  ecosystem** (issue #142, layer 3 of the language-bias investigation).
  Their environment sections opened on PEP 668 and virtualenvs, and the one
  worked verify example was `.venv/bin/pytest`, so a planner had exactly one
  ecosystem to pattern-match against. The environment facts are now
  language-neutral (apt + sudo, egress) with the universal workspace-root
  verify contract kept prominent, and per-ecosystem specifics moved into a
  short reference block the model reads one entry of. Python is the first
  entry — its PEP 668 guidance is unchanged in substance, just no longer the
  framing for everyone else. Ecosystems covered so far: Python,
  JavaScript/Node, TypeScript, Go.

- **Leftovers from the 0.2.0 `sdxloop` → `sbxloop` rename are gone.** The
  exception base class is now `SbxloopError` (was `SdxloopError`) across all
  51 references — a breaking rename for anything importing
  `sbxloop.errors.SbxloopError` directly, consistent with the no-shim
  cutover 0.2.0 already made; it was never exported from `sbxloop.__all__`,
  so the top-level public API is unchanged. CI's push trigger is `sbx/**`
  (was `sdx/**`, which never matched the branch convention README
  documents, so branch pushes silently skipped CI), and the stale
  `.sdxloop/` ignore entry and empty state dir were removed. Pre-0.2.0
  changelog entries keep the old name: they record what actually shipped.

- **Provisioning is no longer fully serial** (issue #127). The agent and
  github sandboxes — which share nothing but the host workspace dir — now
  create, policy, secret, and probe on parallel threads, and the engine
  installs both workers concurrently as well, cutting the fixed pre-run
  startup tax roughly in half on github-enabled runs. The prebaked-template
  verification collapsed from three `sbx exec` round trips (manifest read,
  import/version check, entrypoint smoke) into one in-sandbox script, so the
  templated happy path costs a single probe per sandbox. Supporting changes:
  `EventBus.publish` is now thread-safe (subscriber invocations stay
  serialized), the workspace wheel build is lock-guarded so concurrent
  installs share one `uv build`, and a provisioning failure on either thread
  still drains the other before rolling back everything the attempt created.

### Fixed

- **Agent glob/grep no longer die on 16 KiB-page sandboxes** (issue #122).
  The Copilot CLI's bundled ripgrep is a musl-static jemalloc build compiled
  for 4 KiB pages; on guests with a larger page size (16 KiB is common for
  Apple-silicon microVMs) every `glob`/`grep` tool call aborted with
  `<jemalloc>: Unsupported system page size`, silently stripping the agent —
  and especially the shell-less read-only critic — of its search tools. Three
  layers now cover it: the worker detects the guest page size before each
  Copilot session and reroutes glob/grep to the system ripgrep via the CLI's
  documented `USE_BUILTIN_RIPGREP=false` escape hatch (an operator-set value
  is never overridden; a `sandbox.tooling_warning` event records the reroute,
  or the degradation when no `rg` exists); provisioning's dev-tools ensure
  apt-installs `ripgrep` on non-4-KiB guests that lack one (probe-first — a
  4 KiB guest or a template that ships `rg` costs no apt and no network); and
  a new `page-size` conformance probe reports the guest page size and
  fallback readiness under `sbxloop doctor --deep`.

- **A read-only critic that lost its tooling can no longer emit a confident
  clean verdict (#123).** Field failure (the same incident as #122): a
  SCRUTINIZE session whose `glob`/`grep` calls crashed (and whose `shell`
  was denied by the read-only allowlist, as designed) was left with only
  `view` — and still returned `{"verdict": "pass"}` indistinguishable from
  a thorough review. Agent sessions now tally permission denials and
  tool-call failures (`SessionHealth` on the job result; each denial also
  emits an `agent.permission_denied` event), and the critic phases apply a
  degraded-tooling guard: a `pass`/`accept` from a session with failed tool
  calls is not trusted — the phase re-runs once in a fresh session that is
  confronted with the failures and must account for the reduced coverage
  (a transient crash gets its second chance), and a still-degraded clean
  verdict is downgraded to `revise`/`reject` with the tally in the feedback.
  Denials alone never trigger the guard (a critic probing `shell` is the
  barrier working, not a broken session). Every critic phase row now
  persists the session's `tooling_health` and a `downgraded` marker, and a
  downgrade emits a `phase.end` event with `status="degraded"`, so crippled
  critic runs are auditable live and after the fact. The scrutinize/validate
  prompts also tell the critic up front to report lost coverage instead of
  claiming verification it could not perform.

### Added

- **Well-known package registries are declarable out of the box.** Runs
  building Ruby/Node/Rust/Go projects used to die in `bundle install` /
  `npm install`: the default `[policy] allow` is empty, so a plan declaring
  `rubygems.org` failed validation and the sandbox blocked the host. A
  curated set of read-only registries — RubyGems (`rubygems.org`,
  `index.rubygems.org`), npm/yarn (`registry.npmjs.org`,
  `registry.yarnpkg.com`), crates.io (`crates.io`, `static.crates.io`,
  `index.crates.io`), and the Go proxy (`proxy.golang.org`,
  `sum.golang.org`) — is now always in-bounds for plan-declared egress
  (`policy.WELL_KNOWN_REGISTRY_DOMAINS`). They stay grant-late: unreachable
  unless a plan declares them with a justification, every grant
  event-logged, and `[policy] deny` still blocks them. The plan/execute
  prompts now name the set so planners declare what the toolchain needs.

- **Interactive chat with a running loop.** `sbxloop run` is no longer
  watch-only: the TUI grows a chat form (keystrokes captured in cbreak mode,
  the in-progress line rendered inside the pinned status panel; `--no-tui`
  reads plain stdin lines). A submitted message queues on the engine and is
  absorbed at the next phase boundary — the same checkpoint cancellation
  uses — where the agent pauses and answers it in a fresh read-only STEER
  session that may inspect the workspace. The verdict decides the course
  change: `continue` (answer only), `steer_task` (the current task re-plans
  immediately with the user's guidance as feedback, spending no
  revision/replan budget — user direction is not a failure), or `steer_run`
  (standing guidance injected into every later plan/execute prompt,
  persisted in a new `runs.user_guidance` column so resumed runs keep their
  direction; the schema migrates in place). Every chat turn is event-logged
  (`chat.message` / `chat.reply` / `chat.action`, query with
  `sbxloop logs RUN --type chat.`) and recorded as a `steer` phase attempt;
  a failed steer never fails the run. The status panel shows
  queued/answering messages, and `--chat/--no-chat` (on `run` and `resume`,
  default on with a TTY) controls the whole feature.

- **Transcript panels name the responding agent.** Agent feedback bubbles
  used to be titled a generic `agent <time>`, so you couldn't tell which
  Copilot session was speaking. Each phase now stamps its persona
  (`decomposer`, `planner`, `executor`, `scrutinizer`, `validator`) onto its
  job's `agent.*` events host-side (the in-sandbox worker doesn't know which
  phase it serves), the TUI header shows it, and `sbxloop logs` lines carry
  it as `[<name>]`. Events without a name (older runs) keep the `agent`
  title.

- **`sbxloop list-models`** — lists the models the GitHub Copilot SDK gives
  the authenticated subscription access to, straight from the SDK's
  `list_models()` API on the host (no sandbox): model id, display name,
  billing multiplier, context window, vision support, reasoning-effort
  levels (default marked), and policy state, with the configured `model`
  highlighted and a warning when it is not in the list. `--json` emits the
  SDK's raw model dicts for scripting. The SDK is optional host-side — the
  new `sbxloop[copilot]` extra installs it, and the command explains that
  when it is missing. Auth uses the SDK's normal env chain
  (`COPILOT_GITHUB_TOKEN` → `GH_TOKEN` → `GITHUB_TOKEN`, `./.env`
  included), and failures carry an auth diagnostic naming which token env
  var was visible.

- **Prebaked sandbox templates + `sbxloop bake` (#48).** `sbxloop bake` runs
  the worker install ladder once in a scratch sandbox (plus a best-effort
  Copilot runtime pre-cache) and persists the result with `sbx template save`. With `[sandbox] template` pointing at the baked ref, provisioning
  verifies the baked worker with fast probes (bake manifest → version match
  → entrypoint smoke) and skips the per-run install entirely; any probe
  failure falls back to the existing install ladder, so a stale template
  degrades to today's behavior instead of failing the run. Runs emit a
  `sandbox.prebaked` event either way, and `sbxloop doctor` warns when the
  configured template was baked with an older worker (re-run `sbxloop bake`) or is missing from `sbx template ls`.

- **`sbxloop doctor` now runs an sbx conformance suite** (#52): every
  field-learned assumption about sbx semantics is a named probe with a
  machine-checkable verdict — secret-env visibility under `exec`, the
  exec error channel, `cp <dir>/.` semantics, workspace-mount
  discoverability, template python3-venv presence, custom-secret keying and
  the parseable exists-error scope, plus cheap CLI-surface probes. Verdicts
  are cached per `sbx version` in the state dir; `doctor` runs the cheap
  probes and serves live-sandbox verdicts from the cache, `doctor --deep`
  boots one scratch sandbox for the full suite. When a verdict flips —
  against the verdict this sbxloop build depends on, or against a prior sbx
  version's cache — doctor warns loudly, naming the dependent behavior.
  Provisioning's existing checks (secret visibility, mount discovery) now
  feed the same cache, so ordinary runs keep the verdicts fresh for free.

- **`sbxloop secrets` command group** — proactive lifecycle management for
  the sbx custom-secret registrations sbxloop owns
  ([#55](https://github.com/brettbergin/sbxloop/issues/55)):

  - `secrets list` enumerates the tracked registrations (the Copilot token)
    across scopes and flags pre-collision state: stale registrations owned
    by dead run sandboxes, wrong host bindings from older versions, and
    foreign-scope conflicts. Enumeration tries `sbx secret ls` and falls
    back to a transient set-custom collision probe (the exists-error names
    the owning scope) when the build doesn't support listing.
  - `secrets clean` removes stale sbxloop-owned registrations — dry-run by
    default (`--apply` to execute, `--all` to include healthy ones). Never
    touches foreign scopes or the built-in `github` service secret.
  - `secrets rotate` replaces the registration with a new token value in
    one step (read from the environment/.env or a hidden prompt, never
    argv), warns when live sandboxes may still hold the old token, and
    verifies which secret strategy (proxy vs plain-env fallback) the next
    run will use via a throwaway sandbox (`--no-verify` skips).

- The 0.1.3 secret-collision recovery logic now lives in a shared module
  (`sbxloop.sbx.secretstate`); provisioning and the `secrets` commands use
  the same field-hardened implementation.

- **Plan-declared, least-privilege network egress** (#49). The PLAN phase
  may now declare external domains a task needs during EXECUTE (each with a
  justification) via a new `egress` field in the plan schema. Declarations
  are validated against operator-set bounds — a new `[policy]` section in
  sbxloop.toml with `allow`/`deny` domain patterns (exact, `*.wildcard`, or
  `*`) — and out-of-bounds requests fail plan validation with a remediation
  hint. In-bounds grants are applied grant-late (`sbx policy allow network <domain> --sandbox <agent>` at EXECUTE entry, so resumed runs re-grant on
  their fresh sandboxes) and every grant/refusal is emitted as a
  `policy.allow`/`policy.deny` run event, making the persisted event log an
  egress audit trail (`sbxloop logs RUN --type policy.`). `sbxloop config policy` renders the effective per-phase policy. sbx 0.35 has no
  revocation primitive, so grants persist for the sandbox's lifetime but
  never outlive a run (sandboxes are removed at run end).

- **`keep_on_failure`** (config + `--keep-on-failure`) — successful runs clean
  up as always; failed runs (task failures and infra crashes alike) leave the
  sandbox pair alive, mark the run `kept_reason="debug"` in the state DB, emit
  a `run.keep` event, and print a hint naming the sandboxes and the shell
  command to inspect them. `--keep-sandboxes` runs are now marked
  `kept_reason="manual"` so `sandbox prune` respects them too.

- **`sbxloop shell <run> [--role agent|github] [-c CMD]`** — opens an
  interactive shell (or runs a one-off command) inside a run's sandbox after
  verifying liveness via `sbx ls`. Works for kept, in-flight, and leaked
  sandboxes; the inner exit code is passed through.

- **`sbxloop sandbox prune`** — garbage-collect orphaned `sbxloop-*` sandboxes
  left behind by crashed hosts or killed runs, by cross-referencing `sbx ls`
  against the state DB. Dry-run by default; `--force` removes, `--min-age`
  (hours, default 1) guards against racing live runs, `--include-kept` also
  prunes kept-for-debugging sandboxes. `sbxloop doctor` now reports the
  orphan-candidate count. The runs table gained a `kept_reason` column (the
  kept-sandbox taxonomy prune respects; applied as an in-place migration).

### Changed

- **Provisioning batches network policy grants into one `sbx` call.**
  `sbx policy allow network` takes RESOURCES as a comma-separated list
  (documented since the first sbx CLI reference), so provisioning and
  template baking now grant all of a sandbox's allow-domains in a single
  invocation instead of one CLI round-trip per domain (13 for the agent
  sandbox). Plan-declared egress grants are unchanged: still one call per
  domain, each individually event-logged.
- Coverage moved out of the default pytest options and into CI /
  `make test-cov`: tracing cost ~15% of full-suite wall time, and the
  `--cov-fail-under` gate made every partial run (single file, `-k`,
  `--pdb`) fail spuriously. `make check` still enforces the 85% gate.
- The test suite runs parallel by default (pytest-xdist, `-n auto` with
  work-stealing): ~8min → ~2.5min locally. Pass `-n0` for a serial run when
  debugging with `-s`/`--pdb`. Two wheel-resolution tests were made hermetic:
  they no longer plant or depend on wheels in the real installed package's
  `_vendor/` directory, which raced with the build hook and real pip installs
  under parallel execution.
- **Releases are now fully automated** (aligned with the entrygraph release
  strategy): every merge to `main` runs the check suite, auto-bumps the patch
  version via a new `vX.Y.Z` git tag, and publishes both distributions to
  PyPI — no more release PRs or manual version edits. Package versions are
  derived from git tags by hatch-vcs (`dynamic = ["version"]`); the exact
  `sbxloop-worker==X.Y.Z` lockstep pin is injected into the host wheel
  metadata at build time. See [RELEASING.md](RELEASING.md).

### Fixed

- Artifact listings and delivery no longer silently drop dot-path artifacts
  (#67). `artifact_files` excluded every file with any dot-prefixed path
  component, so agent-produced `.github/workflows/*.yml`, `.gitignore`,
  `.env.example` and friends vanished from the run summary, `sbxloop artifacts`, and delivered PRs — with no indication anywhere. The exclusion
  is now a targeted denylist (`.git`, `.sbxloop` by default, matched as path
  components at any depth), tunable via `[artifacts] exclude` in config, and
  exclusions are always surfaced: the `run.artifacts` event carries per-entry
  excluded counts, the run summary and `sbxloop artifacts` print an
  "N file(s) excluded (…)" note, and delivery PRs list what was left out.
- The pinned status panel now shows the whole decomposed task list up
  front (#63). Previously a task's row only appeared when it started, so
  t2…tn were invisible until each prior task finished. The engine now
  announces the full roster (ids, titles, states) right after
  decomposition — and on resume, restoring each task's persisted state —
  and the TUI renders not-yet-started rows as `waiting` until their turn.
- Ctrl+C now finishes cleanly instead of surfacing tracebacks/`Aborted!`.
  Building on the #64 signal handlers and the #68 engine quiesce, the CLI
  handles the interrupt in both display modes: after the sandboxes are
  torn down it prints an `interrupted` notice and a `sbxloop resume RUN_ID` hint (interrupted runs stay resumable) and exits 130; a second
  Ctrl+C during teardown force-quits, deferring leftover sandboxes to
  `sbxloop sandbox prune`. The registry's `cleanup_all` additionally
  respects `--keep-sandboxes` pairs on abnormal exit instead of deleting
  sandboxes the run DB just marked as kept.
- **P4 papercut batch (#68):**
  - `--keep-sandboxes` is now tri-state
    (`--keep-sandboxes/--no-keep-sandboxes`, default "no override") like
    `--report`/`--deliver` already were, so a config-file
    `keep_sandboxes = true` can be forced off from the CLI.
  - `cancel` refuses runs already in a terminal state
    (`completed`/`failed`/`cancelled`) with a clear message instead of
    silently rewriting their recorded state to `cancelled`.
  - `logs --follow` no longer spins forever on a run whose driving process
    died hard (state stuck non-terminal): after `--stale-after` minutes
    (default 10; 0 follows forever) with no activity — no new events and no
    state change — it prints a note and exits.
  - Provisioning rollback now best-effort unregisters the secrets the
    failed attempt registered, symmetric with sandbox removal, so the next
    run starts clean instead of depending on collision-recovery
    scope-parsing heuristics against a registration owned by a
    now-deleted sandbox scope.
  - Ctrl-C in the TUI now signals the engine thread (via a new
    `LoopEngine.request_cancel()`, checked at the same phase boundaries as
    store-level cancellation) and joins it briefly before sandbox cleanup,
    instead of tearing sandboxes down under an engine still mid-`sbx exec`.
    The interrupted run's persisted state is untouched, so it stays
    resumable. Composed with the #64 signal handlers: the cleanup registry
    runs a driver-set quiesce callback before signal-triggered teardown,
    so SIGINT/SIGTERM stop the engine first, then remove the sandboxes.
  - The `Hook` protocol docstring no longer claims hooks "must not raise":
    the bus has always contained and logged subscriber exceptions, so hook
    authors need no defensive boilerplate (hooks should still be fast).
  - `status <run>` now prints the run's sandbox pair names with their
    current liveness per `sbx ls`, plus a `sbxloop shell` hint when one is
    running — no more reconstructing `sbxloop-<run>-agent` by hand.
- SIGTERM during a TUI-mode run no longer leaks the sandbox pair (#64). The
  TUI runs the engine on a background thread, and the cleanup registry's
  handler installer latched itself as "installed" *before* discovering it
  was off the main thread — so signal handlers were never installed and
  could never be installed later, and SIGTERM's default disposition kills
  the process without running the atexit hook. The latch now only sets
  after handlers actually install (later main-thread registrations retry),
  and the CLI explicitly installs the handlers from the main thread before
  handing the engine to the TUI's background thread. A TUI run receiving
  SIGTERM now stops and removes both sandboxes and exits 143; the lazy
  registration path remains as a fallback for library embedding.
- **Delivery now batches blob creation into O(1) worker jobs (#66).**
  `deliver_workspace` used to submit one `github.op` job per file — a full
  job cycle (`sbx cp` job JSON in, fresh worker process, `sbx cp` result
  out) per blob POST, so a 200-file workspace meant 200+ sequential job
  round trips and tens of minutes of delivery. A new `blobs.create_many`
  worker op receives the whole file manifest (base64-embedded in the job
  JSON) and performs the per-file blob POSTs inside the github sandbox,
  chunked only by a payload-size cap (4 MiB of base64 per job), with the
  job timeout scaled to the manifest size. The worker streams
  `gh.op_progress` events every 10 blobs so long deliveries stay visibly
  alive in the TUI, and a per-file failure names the failing file (and its
  position in the manifest) in the `run.deliver` error event. The e2e
  workflow gains a gated 50-file delivery smoke asserting the PR opens
  under a 120 s budget (`E2E_DELIVER_REPO` repository variable).
- The poll transport's event tailing is now binary-safe and its completion
  check parses events instead of substring-matching
  ([#65](https://github.com/brettbergin/sbxloop/issues/65)). Chunks are
  fetched base64-encoded and the byte offset advances by decoded byte count,
  so `\r\n` in worker output can no longer drift the offset (duplicating or
  dropping event lines), and a `tail -c` boundary that splits a multibyte
  UTF-8 character is held by an incremental decoder instead of crashing the
  host-side decode. Polling now ends only on a *parsed* `worker.end` event —
  an agent message whose payload merely contains the literal string
  `"worker.end"` no longer terminates the poll early.
- `resume` now runs under the config the run was started with (#60). The
  full config has always been persisted in the runs table, but resume drove
  with whatever `load_config()` produced at resume time — so editing
  config (or resuming from a different directory) silently changed budgets,
  model, or GitHub toggles mid-run, and could relocate the run into a
  fresh, empty workspace. Resume now rehydrates the persisted config
  (tokens still come from the current environment; `state_dir` stays where
  the run was found; the `keep_sandboxes`/`keep_on_failure` debug toggles
  stay resume-time choices so a crashing run can be resumed with keep
  flipped on), pins the workspace from the runs table instead of
  recomputing it, refuses a workspace mismatch, and reports any difference
  from the current on-disk config as a `run.config_drift` event.
- The fake sbx used in tests now scopes `pkill` to the sandbox's own
  processes, emulating the microVM boundary. A timeout kill's host-wide
  `pkill -f sbxloop_worker.*<job_id>` could TERM other tests' live worker
  processes under pytest-xdist (job ids repeat across tests), surfacing as
  spurious `exec rc=-15` / no-result failures in unrelated tests.
- Worker wheel builds no longer mutate the live source tree, removing the
  remaining parallel-test flakes and a version-skew hazard. Both
  `resolve_worker_wheel`'s workspace build and the packaging test built in
  place, which rewrote the packages' hatch-vcs `_version.py` files (raced
  by every worker subprocess import under pytest-xdist) and deleted/rebuilt
  the wheels in `src/sbxloop/_vendor/` mid-suite. Builds now run against a
  private temp copy with the version pinned to the host's
  (`SETUPTOOLS_SCM_PRETEND_VERSION`), which also guarantees the built wheel
  passes the install ladder's lockstep version check even when git HEAD has
  moved since the environment was last synced.
- A run resumed while a task was checkpointed `validating` no longer asks
  the VALIDATE judge to rule without evidence (#61). The verify-command
  transcript lived only in memory on the `PhaseRunner` (a class-level
  default of `"(verification not run)"`), so a fresh process entering
  VALIDATE rendered the prompt with the placeholder instead of the real
  results. VERIFY now persists its full command transcript on the
  `phase_attempts` row and VALIDATE reads it from there — the single source
  of truth for both fresh and resumed runs — and the class-level mutable
  state is gone. A checkpoint whose verify row predates this change (no
  stored transcript) rewinds to `verifying` on resume; VERIFY is mechanical
  and idempotent, so the evidence is cheaply repopulated.
- A delivery infrastructure failure can no longer mark a completed run as
  failed (#59). `--deliver` runs after the run has succeeded; worker- and
  sbx-level errors raised by the delivery op jobs (`WorkerError`,
  `WorkerTimeoutError`, `SbxError`) were escaping the delivery guard and
  leaving the run stuck in `finalizing`, reported as failed. Delivery now
  contains every sbxloop error, keeps the loud `run.deliver` event, and the
  run finishes `completed` as documented.
- GitHub progress reporting (`--report`) now actually reports (#58). The
  tracking issue was never created: the hook subscribed to run lifecycle
  events that are emitted before the github sandbox exists and after it is
  torn down. Run start/end are now explicit `open_run`/`close_run` calls
  made while the sandbox is alive; task-end comments flow via the bus as
  before, and the final summary posts before teardown. A resumed run
  re-finds its existing tracking issue instead of opening a duplicate.

### Security

- sbx infra failures can no longer silently downgrade the secret strategy
  (#63). The secret visibility probe only accepts a clean `test -n` answer
  (exit 0/1): an sbx-level failure or any other exit code is retried once
  and then **fails provisioning loudly** (with a distinct
  `sandbox.secret_probe_error` event) instead of being misread as "proxy
  secret invisible" and auto-writing the token into the VM. Supporting
  changes: `SbxCLI.exec` now classifies recognizable sbx-level stderr
  shapes (sandbox not running, daemon unreachable, transport failures) and
  raises `SbxError` for them instead of returning them as command results;
  mount discovery still degrades to harvest mode on a broken probe but its
  `sandbox.workspace_mount` event now distinguishes `probe="error"` from
  `probe="answered"` (and only clean answers refresh the conformance
  cache); `policy_check` raises on invocation failure rather than
  reporting infra trouble as "blocked", and `doctor` labels that case as a
  check error, not a policy verdict.
- **The read-only critic barrier is now allowlist + default-deny (#62).**
  SCRUTINIZE/VALIDATE sessions previously denied only `{"shell", "write"}`
  permission kinds and approved everything else — an unverified denylist,
  so an SDK rename or a new mutating kind would have silently handed the
  critic approve-all over the workspace it reviews. The barrier now allows
  only known-read kinds (`read`, `url`) and rejects everything else with
  feedback naming the denied kind, so unknown kinds fail closed (worst
  case: the critic loses a read capability and says so). The full `kind`
  vocabulary was field-verified against github-copilot-sdk 1.0.8 (`shell`,
  `write`, `read`, `mcp`, `url`, `memory`, `custom-tool`, `hook`,
  `extension-management`, `extension-permission-access`), and
  `sbxloop doctor` now compares the installed SDK's vocabulary against
  that snapshot, warning loudly on drift after an SDK bump.
- Secret values no longer leak through error text or the process-observable
  argv carried on `ExecResult`/`SbxError`: the value passed to
  `sbx secret set-custom --value` is masked (`***`) in every observable copy
  of the invocation, so provisioning failures cannot print the Copilot PAT
  into terminals, logs, or events (#57). The remaining `ps`-visibility of
  the live subprocess argv needs stdin support in sbx itself and stays
  tracked in #57.
- The `ps`-visibility half of #57 is now a doctor conformance probe
  (`secret-value-stdin`, cheap tier): desk-verified against the sbx docs and
  release history through v0.37, `sbx secret set-custom` accepts the secret
  only via `--token`/`--value` on argv (both documented as "less secure:
  visible in shell history") — no stdin path exists, so the exposure window
  cannot be closed from sbxloop's side yet. The probe checks
  `set-custom --help` on every `sbxloop doctor` run and alarms the moment an
  sbx upgrade grows a stdin path, naming the switch-over as the fix.
  `exec_interactive`'s missing-binary error now carries the redacted argv
  copy like every other observable path.

## [0.2.0] — 2026-07-23

### Changed

- **Project renamed: sdxloop → sbxloop — hard cutover, no compatibility
  layer.** The underlying Docker product is the `sbx` CLI; the name now
  matches. Everything renames with it: the distributions (`sbxloop`,
  `sbxloop-worker`), import names (`sbxloop`, `sbxloop_worker`), the CLI
  command (`sbxloop`), the env prefix (`SBXLOOP_*`), the config file
  (`sbxloop.toml` / `[tool.sbxloop]`), the state dir (`.sbxloop/`), and
  sandbox name prefixes (`sbxloop-<run>-*`). The old `sdxloop` /
  `sdxloop-worker` PyPI packages are frozen at 0.1.10 and will receive no
  further releases. Migration: `pip uninstall sdxloop sdxloop-worker && pip install sbxloop`, rename `SDXLOOP_*` env vars / `.env` entries and
  `sdxloop.toml`, and optionally rename `.sdxloop/` state dirs to
  `.sbxloop/` to keep old run history visible.

## [0.1.10] — 2026-07-23

### Changed

- The live run view is now a chat-style transcript: agent messages render
  as markdown panels (fenced \`\`\`json blocks syntax-highlighted and
  word-wrapped instead of truncated), errors as red panels, tool calls as
  compact colored lines, lifecycle events as dim one-liners. Streaming
  deltas, heartbeats, and stdout noise no longer flood the feed (they
  remain available via `sdxloop logs`). `--no-tui` prints the same
  chat-style entries sequentially.
- The secret-env fallback is one concise line and a single
  `sandbox.secret_env_fallback` event; explicit `plain-env` strategy no
  longer runs (and spuriously fails) the shell visibility check.

## [0.1.9] — 2026-07-23

### Fixed

- Runs work out of the box on real sbx: field testing confirmed the sbx
  secret proxy never exposes env vars to `sbx exec` processes (only to
  the interactive agent sessions sbx launches). Under the default proxy
  strategy, provisioning now auto-falls-back to the in-VM env file for
  any sandbox whose secret env is invisible, emitting a
  `sandbox.secret_env_fallback` warning about the tradeoff. If sbx later
  injects secrets into exec sessions, the verification passes and tokens
  stay out of the VM automatically.

## [0.1.8] — 2026-07-23

### Fixed

- Copilot session auth: the worker now runs under a login shell
  (`sh -lc`) so sbx-injected secret env vars reach it, and it also loads
  `/etc/sandbox-persistent.sh`. Provisioning verifies the secret env is
  visible in each sandbox and emits a `sandbox.secret_env_missing`
  warning (with a plain-env remediation hint) when it is not. Copilot
  session failures now append an auth diagnostic stating whether
  COPILOT_GITHUB_TOKEN is missing, well-formed, or looks like the sbx
  proxy sentinel (which the SDK's client-side token validation cannot
  use - switch to `secret_strategy = "plain-env"` in that case).

## [0.1.7] — 2026-07-23

### Fixed

- Job files staged into sandboxes are now world-readable: they were
  created 0600 by the host tempfile machinery and `sbx cp` preserves the
  mode, so the in-sandbox `agent` user could not read its own job files
  (Errno 13) and every job died before producing a result.

## [0.1.6] — 2026-07-23

### Fixed

- "produced no result file" failures are now diagnosable: the worker's
  stderr is drained (also fixing a potential pipe-deadlock for chatty
  workers) and the error carries the exec exit code, stderr tail, and
  the last lines of the in-sandbox events file.
- Worker installation now ends with an entrypoint smoke check:
  `python -m sdxloop_worker run` against a missing job must exit 64.
  Importing the package proves nothing about the entrypoint executing —
  broken entrypoints now fail at install time with a full traceback
  instead of as silent no-result jobs.

## [0.1.5] — 2026-07-23

### Fixed

- Worker wheels are staged into the sandbox under their canonical
  filename: pip validates the name-version-python-abi-platform structure
  of the wheel FILENAME and refused the previously renamed
  `sdxloop_worker.whl` ("Invalid wheel filename"). A new regression test
  runs real pip against the real wheel through the fake sbx.

## [0.1.4] — 2026-07-23

### Fixed

- Worker installation no longer dies when the sandbox template lacks
  python3-venv: it self-heals via `sudo apt-get install python3-venv python3-pip` and, failing that, falls back to a user-site pip install
  under the system python3 (handling PEP 668 externally-managed
  environments). Install/exec errors now include stdout as well as
  stderr — sbx exec surfaces some errors on stdout, which previously
  produced blank "rc=1" messages.

## [0.1.3] — 2026-07-23

### Fixed

- Secret provisioning now survives sbx's real conflict semantics: custom
  secrets are keyed by env name (one host per env), so the Copilot token
  binds to `api.github.com` only (the token-exchange host; the exchanged
  Copilot token lives in SDK memory). Exists-conflicts parse the owning
  scope out of sbx's error, try removal candidates from most to least
  specific, and NEVER fail provisioning — worst case the existing value
  is kept with a warning.

## [0.1.2] — 2026-07-23

### Fixed

- Provisioning no longer fails with "secret exists" on re-runs and resumes:
  sbx refuses to overwrite existing secrets, so the provisioner now removes
  and re-sets them (rotated tokens take effect). When removal is rejected,
  the existing value is kept with a warning instead of failing the run.

## [0.1.1] — 2026-07-23

### Changed

- `app_name` now defaults to empty: sdxloop shares the user's normal sbx
  application state, so `sbx login` and `sbx policy init balanced` apply
  directly. Isolation via `--app-name` is opt-in (and documented to need
  its own login/policy init). Previously the default isolated state
  silently triggered Docker's browser login on first `sdxloop doctor`.
- `sdxloop doctor` prints progress lines for slow checks (including a
  heads-up that Docker may open a browser for auth) and sanitizes
  multi-line sbx error output so the results table renders cleanly.

### Added

- `.env` support: the CLI and `LoopEngine` automatically load `./.env`
  (via python-dotenv) for the two PATs and `SDXLOOP_*` settings. Real
  environment variables always take precedence, and explicit `env=`
  mappings passed to `load_config` stay hermetic. A documented
  `.env.example` ships in the repo.

## [0.1.0] — 2026-07-22

Initial release.

- **Sandbox-pair primitive**: every run provisions an agent sandbox
  (`COPILOT_GITHUB_TOKEN` only, proxied to the Copilot API hosts) and a
  github-ops sandbox (`GH_TOKEN` only, built-in secret service), both under
  the balanced network policy with per-role allow rules, guaranteed cleanup
  (context manager + atexit/signal registry), and `--app-name` isolation.
- **Loop engine**: DECOMPOSE → PLAN → EXECUTE → SCRUTINIZE → VERIFY →
  VALIDATE with revision/replan budgets, read-only critic sessions,
  mechanical verification, SQLite checkpointing after every transition,
  crash-safe `resume`, wall-clock budgets, and cancellation.
- **Worker runtime** (`sdxloop-worker`): file-based job protocol with JSONL
  event streaming, GitHub Copilot SDK backend (lazy `[copilot]` extra),
  deterministic echo backend for testing, GitHub ops via gh CLI or
  pure-stdlib REST.
- **Worker delivery**: the host wheel embeds the worker wheel, so sandbox
  provisioning works before/without PyPI.
- **CLI**: `run` (rich live TUI), `resume`, `cancel`, `status`, `logs`,
  `sandbox ls/rm`, `config show`, `init`, `doctor`.
- **CI/CD**: ruff format+lint, mypy strict, pytest with 85% coverage gate
  (3.11–3.13), build with vendored-wheel assertion, PyPI Trusted Publishing
  on tags, manually-dispatched real-sbx e2e workflow.
