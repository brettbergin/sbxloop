# Changelog

All notable changes to sbxloop are documented here. The project adheres to
[Semantic Versioning](https://semver.org/) and both distributions (`sbxloop`,
`sbxloop-worker`) release in lockstep.

## [Unreleased]

### Fixed

- Daemon: an operator `!sbx cancel` is no longer settled as a failed
  attempt (field: cancelled from Discord → "failed; 1 attempt(s) left" →
  re-run fresh after the 15-minute backoff, and counted toward the circuit
  breaker — #246). The item now settles as **cancelled**: a new terminal
  work-item state with no automatic retry and no breaker count, reported on
  the source with attribution ("cancelled by Discord user `x`"; GitHub:
  comment + in-progress label removed, trigger left for a human), while the
  run itself stays resumable — the finish card and source comment say
  `sbxloop resume RUN`. New `!sbx cancel --retry` re-queues the item for a
  fresh run instead, and `!sbx retry <item>` reruns any cancelled or
  abandoned item (a human retry resets the attempt budget and skips the
  failure backoff; the daily cap still applies).
- Discord bridge with `--once` and other short-lived runs (#236): a run
  that started and finished before the gateway connected lost its headline
  and everything after it (the pump only knew the *active* run's item).
  Events are now buffered per run until the bridge is ready, and `close()`
  waits (bounded, `DRAIN_WAIT_S`) for the pump to post what is already
  queued, so a short run's chronology is complete instead of truncated at
  process exit.

### Changed

- Discord `chronology_level = "normal"` no longer streams every tool call
  (#235): a burst is digested into one line edited in place — count,
  per-tool breakdown, last command, failure count — closed by the next
  agent message, phase or task boundary. A trailing run of near-identical
  commands is collapsed to `⚙ bash x17 similar commands` with a "may be
  stuck; `!sbx cancel` stops the run" nudge; the `agent.tool_cap` ceiling
  (#228) is surfaced too. Failed calls keep their own detail block. The
  previous stream-everything behaviour is `"verbose"`; the full stream stays
  in `sbxloop logs`.

### Added

- `sbxloop deliver <run>` (#223): deliver — or re-deliver — a completed
  run's artifacts as a PR without re-running the work. End-of-run delivery
  was a one-shot side effect with no retry path (field failure `rgwp5z40x`:
  every task passed, delivery failed on the empty-repo 409, and `resume`
  refuses completed runs). The command provisions a github-ops sandbox only,
  reuses the run's persisted config with `--repo`/`--deliver-base`/
  `--deliver-draft`/`--create-repo` overrides on top, runs the normal
  `ensure_repository` + `deliver_workspace` path, and emits the usual
  `run.deliver` events so `logs` and the finish summary see it; `--report`
  refreshes the run's tracking issue with the PR link. Re-delivering a run
  whose `sbxloop/<run>` branch already exists (a prior partial attempt)
  force-updates the branch and reuses an already-open PR for that head
  instead of failing on the refs POST 422.

- Operator controls for individual daemon work items (#229).
  `sbxloop daemon items` lists every item with state, attempts, pinned run
  and last error; `sbxloop daemon abandon <item> [--reason]` gives one up,
  `sbxloop daemon retry <item>` re-queues an abandoned or cancelled item
  with attempts reset and a fresh plan (not a resume), `sbxloop daemon requeue <item>` unpins a running item from its run so the next dispatch
  starts over (attempts kept). Same controls on Discord as
  `!sbx items|abandon|retry|requeue`. A live daemon
  honors a CLI abandon/requeue of the item in flight within a second — the
  run is cancelled, the operator's decision wins over the run's own outcome
  (no retry, no breaker count), and the source hears `report_abandoned`
  exactly once. The CLI runs in another process and can only flip the row,
  so an abandon or retry leaves a durable `pending_report` debt on it: the
  daemon pays it on its next tick (paused or not) or on the next start —
  the abandon reaches the issue / inbox file (an unclaimed item loses its
  trigger label / leaves `pending/`), a retry re-claims (drops the failed
  label, moves the file out of `failed/`) — exactly once. Recovery also
  closes the dead run's ledger and removes its sandboxes and secrets, as
  does an in-process abandon/requeue of an item queued for resume. Item
  transitions are single conditional statements, so a daemon settling the
  item concurrently cannot have its verdict overwritten by a stale command. Field origin: a spiraling item (#228) could only be
  abandoned by poking `DaemonStore` from Python, and recovery would have
  resumed the doomed plan.

- `sbxloop daemon ctl <status|pause|resume|cancel [--retry]|queue|items|abandon|retry|requeue>`
  — a local control surface for the running daemon (#232). Requests go
  through a file queue in the daemon's `state_dir/daemon/ctl/`, served by a
  daemon thread so `cancel` lands mid-run; a request stamped before the
  daemon's start (or before recovery finished — the control thread only
  starts after `recover()`) is refused, one no daemon picks up within `--timeout` is
  withdrawn, and one the daemon has already taken but not answered is
  reported as pending — the command still executes. Discord's
  `!sbx` and `ctl` share one command dispatcher (`sbxloop.daemon.control`),
  so the two surfaces cannot drift; a ctl cancel/retry is attributed on the
  source as "`<user>` via sbxloop daemon ctl". The bridge still ignores
  bot-authored messages by design.

- The scrutinizer now judges the verify commands as well as the work (#231):
  its prompt carries the exact command list VERIFY will run and the feedback
  the executor was addressing, and its verdict gains `verify_suspect` /
  `verify_suspect_reason` (a flag without a reason is rejected and retried).
  When a revision was triggered by a verify failure (read from the persisted
  verify attempt, not from feedback text) and the scrutinizer passes the
  work while ruling the check itself wrong,
  the engine spends a replan immediately — the planner is told why the old
  check was wrong — instead of burning the remaining revisions against a
  check the executor cannot edit (field failure r567rsm4e: a portable,
  runnable `od | grep` check asserting a column layout `od` never prints).
  A speculative flag on a check that has not failed yet, or one raised with
  no replan budget left, is surfaced but not acted on. Every ruling is a
  `phase.end` event (`status=verify_suspect`, `honored`), shown in the
  Discord bridge and the transcript.

- Discord bridge output is now Discord-native: headline, finished report and
  `!sbx status` as embed cards; agent messages split at paragraph/code-fence
  boundaries (fences re-opened with their language) instead of clipped;
  consecutive tool calls batched into one code block with `✗ exit N` markers;
  a per-run status line edited in place as tasks progress (persisted, so a
  restarted daemon keeps editing the same message); issue/PR/branch as masked
  links (raw URLs in notices wrapped so they don't unfurl); verify failures,
  worker errors, denied permissions, refused egress and tooling warnings
  surfaced; every send disables mentions. New `[discord]` knobs `embeds`,
  `status_line`, `tool_batch_lines`. Pure formatting layer
  `sbxloop.daemon.discord_format` (no discord.py needed to test it).

- `git` is now baseline agent tooling, provisioned on every agent sandbox
  independent of `[sandbox] languages` (#252): the dev-tools ensure probes
  `command -v git` and installs it in the same pooled apt call as the selected
  toolchains; a prebaked template that lacks it is topped up from the single
  prebake probe. `sbxloop bake` records whether git landed and `sbxloop doctor`
  shows a soft "git in template" row.

- **uv-aware Python toolchain** (#250). The `python` toolchain now installs
  `uv` from its pinned, checksum-verified GitHub release (per-arch digests,
  like Node/Go) and a uv-managed Python 3.13 (`python3.13` linked onto PATH),
  and its probe checks the interpreter series rather than mere presence —
  uv-workspace repos declaring `requires-python >= 3.13` (sbxloop's own
  included) previously failed at `uv sync` because the sandbox had neither.
  The plan/execute/decompose prompts carry a "uv projects" note per Python
  block (`uv.lock` present → `uv sync --all-packages` in steps, `uv run …`
  to verify), and the verify-command lint requires `uv run` heads when the
  workspace root has a `uv.lock` (bare `pytest` and `.venv/bin/…` are both
  rejected with the uv remedy; without a lockfile `.venv/bin/…` is
  unchanged). `astral.sh` (uv's installer host) joins the declarable
  registries, and `release-assets.githubusercontent.com` (where GitHub
  release-asset downloads redirect) joins the agent sandbox's provisioning
  allowlist so both downloads actually complete. `sbxloop doctor --deep`
  gains a `python-version` conformance row reporting the template's own
  `python3` against the pinned series.

- Budgets and resources for larger repositories (#253): verify output handed
  to the critic keeps the first 2 KB and the last 4 KB of each command
  (previously the last 1.5 KB only, so a long pytest run's failure summary
  arrived without any assertion text); new `[limits] mem_abort` threshold
  (off by default) fails the task with an explicit "sandbox memory exhausted"
  error the way `disk_abort` does for disk, instead of an in-VM OOM surfacing
  as a confusing test failure; `contrib/presets/large-repo.toml` documents a
  4 h wall clock / 80 tool-call preset and the fact that sbxloop passes no
  CPU/memory sizing to `sbx create`.

- Daemon guardrails now cover recovery, restarts and multi-daemon setups
  (#254, #234). `recover()` no longer dispatches resumes itself: an interrupted
  run is re-queued with its run pinned and the tick resumes it behind the same
  breaker / daily-cap / pause gate as any dispatch. Resumes are recorded in a
  ledger of their own: the daily cap counts them (each is a fresh engine wall
  clock), `!sbx status` shows them, and a new `[daemon] max_resumes_per_item`
  (default 2) bounds them — past it the interrupted run is settled as a failed
  attempt so a plan that keeps getting interrupted cannot burn engine time
  forever. Circuit-breaker state persists in the daemon store, so a
  crash-restart loop no longer resets it. The GitHub claim is now a
  compare-and-swap: the claim comment is posted first and the label swap only
  proceeds if it is the first claim comment of the current trigger cycle, so
  two daemons on one repo cannot both take an issue. The daemon's github
  sandbox is named per state dir (`sbxloop-daemon-github-<hash>`) so a second
  daemon on the host no longer removes the first's at startup. Source polling
  raises on failure and backs off exponentially (up to 30 min); the github
  sandbox is re-provisioned at most once per 5 minutes.

- `[daemon] close_on_success` and `[daemon] tracking_issue` (#251), both
  default true (today's behaviour). `close_on_success = false` leaves a
  delivered source issue open with the new `delivered_label`
  (`sbxloop:delivered`) instead of closing it the moment a draft PR appears
  — the human closes it when the PR merges; a re-trigger or an operator
  re-queue clears the label.
  `tracking_issue = false` skips the per-run tracking issue for GitHub items,
  whose source issue already carries the run's summary comment.

- **Run-directory retention** (#233). Every run leaves
  `<state_dir>/runs/<run>/` behind (the workspace clone under isolation plus
  harvested artifacts) and nothing removed them; an always-on daemon accreted
  clones without bound. New `[daemon] prune_runs_after_days` (default 14, `0`
  disables): the daemon sweeps on its first tick and once a day thereafter,
  and `sbxloop gc [--older-than DAYS] [--dry-run]` runs the same policy by
  hand (`sbxloop.gc`). Only terminal runs past the window are removed — never
  an in-flight/resumable run, a run with kept sandboxes, or one whose last
  delivery failed (its workspace is the only copy of the work). SQLite rows
  are kept; each removal is a `daemon.gc` event on the run (path, bytes,
  age), the daemon reports counts and bytes freed to its frontend, `resume`
  refuses a run whose workspace gc removed, and the workspace-clone finish
  summary prints the retention window. A sweep and a `resume` of the same
  failed run can race across processes: gc writes its marker in one write
  transaction with a re-check that the run is still terminal, then renames
  the directory out of `runs/` (into `gc-pending/`) before deleting it, and
  `resume` leaves the terminal state before touching the workspace and
  re-checks the marker after — so the workspace is never half-removed and
  unmarked, and never pulled out from under a resume.

- **Daemon workspace posture for unattended runs** (#255). Daemon runs
  against a git-checkout workspace use `clone` isolation by default
  (`[daemon] workspace_isolation`): a dirty tree proceeds from committed
  HEAD with a warning instead of `auto`'s refusal, which no human is present
  to answer. Before each fresh run the daemon `git fetch`es the checkout and
  fast-forwards its branch to its upstream (the tracked remote, or
  `origin/<branch>` when none is configured) — never a merge/rebase; diverged or
  colliding trees are left alone and logged, fetch failures warn and run
  from local HEAD (`[daemon] refresh_workspace`, default on). Daemon state
  is anchored to an absolute path outside the workspace: `[daemon] state_dir`, else an explicit `state_dir`, else a pre-existing legacy
  `./.sbxloop/state.db`, else `$XDG_STATE_HOME/sbxloop/<runner-dir-name>`;
  the resolved location is printed at start and the `sbxloop daemon items|abandon|retry|requeue` controls follow the same rule. Per-run clones now point
  `origin` at the source checkout's origin URL instead of the host path
  (metadata only; URL userinfo such as an embedded token is stripped). Docs and the systemd contrib prescribe a dedicated clone
  nobody edits as the daemon's workspace.

- Discord bridge, steering latency made visible (#236): a queued steer gets
  a note under it — `⏳ steer queued — agent is mid-execute on t2 (12/40 tool calls so far); answered at the next checkpoint` — edited in place as
  the phase, task and tool-call count (against the #228 ceiling) move, then
  resolved to picked-up / answered / failed / not-answered-run-ended.

- GitHub op failures carry the HTTP status as a structured field (#221):
  worker `GithubOpError.http_status` (parsed from `gh api` stderr or taken
  from `urllib` `HTTPError.code`), `ErrorInfo.http_status` on the JobResult
  envelope, and host `GithubOpsError.http_status`. The empty-repo bootstrap
  (`409`), missing-repo probe (`404`) and already-absent trigger label
  (`404`) now compare status codes instead of grepping gh's prose — the
  wording-mismatch that broke delivery on run rgwp5z40x (fixed in #219) can
  no longer recur. Message matching remains only as a fallback for a worker
  that predates the field.

### Fixed

- Artifact listings, harvest reports and `--deliver` PRs now honour the
  workspace's own `.gitignore` rules (#249): the name-based
  `[artifacts] exclude` list cannot know a project's `dist/`, vendored
  wheels or generated `_version.py` are build byproducts, so any tree after
  a build/sync delivered them into the PR. Files git would ignore
  (untracked *and* ignored — force-added tracked files still travel) are
  dropped and tallied as `gitignored` in the surfaced exclusion note; the
  probe works on the per-run clone and on harvested copies (which carry
  `.gitignore` but no `.git`), applies only in-tree `.gitignore` files
  (never the operator's global excludes or an enclosing checkout's rules),
  and degrades to the name-based scan when git is unavailable.
  `[artifacts] exclude` remains the operator override on top.
- **Delivery of git-checkout workspaces commits the run's diff, not a
  snapshot** (#248). When the workspace is a git checkout (the per-run
  clone, or an in-place checkout), the PR tree now carries only what the
  run changed relative to its base commit — deletions as `sha: null` tree
  entries, renames as delete + add, executable scripts keeping `100755`,
  symlinks as `120000` — instead of layering every file as `100644` onto
  the base tree, which could never delete a file and flipped every exec
  bit. The diff base is the merge base with the PR's target commit when
  the clone knows it, else the commit the clone was cut from (pinned as
  `refs/sbxloop/base` at clone time). Non-git workspaces still deliver as
  a snapshot; a checkout with no base to diff against falls back to one
  and says so in the PR body. The artifact exclude denylist applies to
  both paths.
- Expected probe answers no longer render as red error panels (#222). The
  `--create-repo` existence probe and delivery's base-ref lookup asked GitHub a
  question whose expected answer was "no", but the worker raised on the 404
  (or the 409 an empty repository returns) and emitted its error event before
  the host could classify the miss as fine — three field runs showed the
  alarm on runs that delivered cleanly. `repo.get` and the new `ref.get` op
  accept `allow_missing: true` and return `{"missing": true}` as an **ok**
  result; `ensure_repository` and the base-commit lookup branch on that data
  instead of sniffing exception messages. `GithubOpError` in the worker now
  carries `http_status` (parsed from gh's trailing `(HTTP NNN)` or urllib's
  `HTTPError.code`) so the miss/error split is a status compare, not a
  substring match.

## [0.6.0] — 2026-08-15

Rollup release: everything below shipped incrementally as the auto-released
v0.2.1–v0.5.86 patch series; v0.6.0 marks the point where the GitHub
integration and existing-checkout workflows were completed and field-verified
end to end.

### Added

- **Per-run workspace isolation for existing git checkouts** (#216, #218).
  When `[sandbox] workspace` points at a git checkout, each run works in a
  self-contained per-run clone on branch `sbxloop/<run_id>` — the checkout's
  working tree, branches, and HEAD are never touched. New
  `[sandbox] workspace_isolation = auto|clone|in-place` (default `auto`):
  `auto` refuses a dirty source tree up front (uncommitted changes would
  silently not travel; sbxloop's own `.sbxloop` state dir is exempt),
  `clone` proceeds from committed HEAD with a warning, `in-place` is the
  old behavior. The finish summary prints where the results live and the
  `git fetch <clone> sbxloop/<run>` command to pull them back. Host-side
  git goes through GitPython (new dependency, host package only).

- **Verify commands are mechanically validated against toolchain
  conventions** (#217, #218) at both decompose and plan acceptance: bare
  `python`/`pip`/`pytest` (use `.venv/bin/...`), bare `rspec`/`rake` (use
  `bundle exec`), bare `phpunit` (use `./vendor/bin/`), and any
  `sudo`/`apt` are rejected with the remedy quoted, costing one JSON retry
  instead of a revision cycle plus an in-VM workaround. Go, Rust, .NET,
  Node, Java, and C/C++ commands are correctly bare and deliberately
  unrestricted. Prompts additionally require verify commands to be
  self-contained — start what they probe, tear it down, never depend on a
  process the executor left running (#212).

- **The GitHub integration is fully drivable from the run command**:
  `--repo owner/name` (overriding `[github].repo`, validated up front),
  `--deliver-base`, `--deliver-draft` (#213), and `--create-repo` /
  `--create-public` (#214) — the delivery repository is probed right after
  provisioning (fail fast on typos, before any work), created on demand
  when explicitly allowed, and an existing-but-empty repository is
  bootstrapped with an initial commit so delivery always lands as a normal
  reviewable PR (#214, #219).

- **Run outcomes are surfaced, not buried in scrollback** (#215): the
  finish summary gains a `github:` section (repository, created-this-run
  marker, tracking issue, delivery PR or failure) and completed runs close
  their tracking issue (`state_reason: completed`); failed runs leave it
  open as the thing still needing a human.

- **Read-only critic sessions may run shell** (#211): `shell` joins the
  critic allowlist so scrutinize/validate sessions can run inspection
  commands directly; everything else, including unknown SDK permission
  kinds, still fails closed.

- **Artifact excludes accept glob patterns** and `*.egg-info` joins the
  defaults (#215) — pip's project-named metadata directory was shipping in
  delivery PRs and no exact component name could catch it.

### Fixed

- **Permission denials and CLI validator refusals no longer masquerade as
  degraded critic tooling** (#211, #212): a rejected call's `success=False`
  completion echo is shielded by its `tool_call_id`, and "Command not
  executed" validator refusals land in a separate non-degrading
  `tool_refusals` tally — previously every denial or refusal downgraded a
  clean critic verdict until the revision budget died.

- **Empty-repository delivery** failed against real GitHub (#219): the ref
  lookup answers HTTP 409 "Git Repository is empty.", not the 404 the
  bootstrap listened for. Both now trigger the bootstrap.

- **sbxloop's own state directory no longer trips the isolation dirty
  refusal** (#218): any command run from inside a checkout drops a relative
  `.sbxloop` there, which is run state, not user content.

### Added

- **`[sandbox] languages = ["dotnet"]` provisions the .NET SDK** (issue
  #164), completing the ten-language set for layer 1 of #140. Availability
  and currency of `dotnet-sdk-*` in the base Debian/Ubuntu archives varies
  by release and is unreliable to depend on, so the SDK comes from
  Microsoft's own builds, pinned to an LTS patch and verified against the
  **sha512** its release metadata publishes (the .NET feed publishes sha512
  rather than sha256). The major is pinned because a project's `global.json`
  can demand an exact SDK and fails hard when it is absent. `DOTNET_ROOT` is
  recorded in the persistent env — a manual, non-package SDK install is only
  half-done without it — along with a telemetry opt-out, which under
  default-deny egress would otherwise only ever be a blocked outbound
  request. At roughly 220 MB the SDK is the strongest candidate in the set
  for `sbxloop bake` rather than a per-run download. Accepted spellings:
  `dotnet`, `csharp`, `c#`, `net`, `dotnet-sdk`. Adds a
  `builds.dotnet.microsoft.com` egress dependency (#141).

- **`[sandbox] languages = ["rust"]` provisions cargo and rustc** (issue
  #143). Debian/Ubuntu do ship `cargo`/`rustc`, but distro Rust routinely
  lags stable by several releases, which breaks edition- and MSRV-sensitive
  projects outright — and rustup is the norm nearly every Rust instruction
  assumes. `rustup-init` is downloaded and checksum-verified per
  architecture rather than piped from `sh.rustup.rs` into a shell, then run
  with `--profile minimal --component rustfmt --component clippy`: the two
  components a plan's verify commands actually reach for, without the large
  docs component nothing here needs. rustup's usual PATH wiring edits shell
  profiles that a bare `sbx exec sh -c` never sources, so the shims are
  linked into `/usr/local/bin` and the toolchain stays in the agent's home
  where `cargo install` can write to it without sudo. Adds
  `static.rust-lang.org` as an egress dependency (#141).

- **`[sandbox] languages = ["go"]` provisions the Go toolchain** (issue
  #153). The official `go.dev` tarball rather than `golang-go`: modules
  frequently declare a `go` directive newer than the distro build, which
  fails the build outright. Pinned and verified against upstream's published
  per-architecture sha256, resolved in-sandbox like the Node entry, and
  installed by replacing `/usr/local/go` rather than extracting over it —
  overlaying two versions leaves a broken tree. `GOTOOLCHAIN` is
  deliberately **not** pinned to `local`: doing so would make a project
  whose `go.mod` demands a newer Go fail outright, which is the very
  distro-lag failure this entry avoids. Left at the default, Go fetches what
  the module asks for during EXECUTE, where a plan can declare the Go proxy
  as egress. Adds a `go.dev` egress dependency at provisioning time (#141).

- **`[sandbox] languages = ["typescript"]` provisions `tsc` on top of Node**
  (issue #150). Toolchain entries can now declare what they are built on,
  and selecting one selects its requirements — TypeScript pulls in the
  JavaScript entry and the registry order guarantees the Node runtime is
  installed before `npm i -g typescript` runs. The compiler is pinned; a
  project with its own `typescript` devDependency will still use that, since
  the global install is for bootstrapping a project from nothing rather than
  for driving any particular build pipeline. Adds a `registry.npmjs.org`
  egress dependency on top of Node's `nodejs.org` (#141) — and because
  provisioning runs before the PLAN phase, a plan declaration cannot satisfy
  it; the README now documents `[sandbox] extra_allow_domains` as the way to
  make installer-based toolchains reachable today.

- **`[sandbox] languages = ["javascript"]` provisions Node** (issue #147).
  The official `nodejs.org` tarball rather than apt: Debian/Ubuntu stable
  ship a Node several majors behind current LTS, which breaks packages
  declaring modern `engines` constraints — a functional failure, not
  cosmetic lag. Pinned to an exact LTS release and verified against the
  upstream `SHASUMS256.txt` digest for the sandbox's architecture, resolved
  in-sandbox so the same config works on arm64 microVMs and amd64 CI
  runners; an unrecognized architecture fails loudly instead of downloading
  a binary that cannot run. The probe checks the pinned **major**, not just
  that `node` exists, so a template carrying an older Node is upgraded
  rather than accepted. Node is extracted into `/usr/local`, which also
  makes it npm's global prefix so `npm i -g` lands on PATH. Accepted
  spellings: `javascript`, `js`, `node`, `nodejs`, `javascript-node`. Adds
  a `nodejs.org` egress dependency (#141).

- **`[sandbox] languages = ["php"]` provisions PHP and Composer** (issue
  #167). apt for the interpreter and — more importantly — the extensions:
  `php-cli` alone passes a `command -v php` check and then fails the moment
  a project or Composer itself needs mbstring or zip, so `php-mbstring`,
  `php-xml`, `php-curl`, and `php-zip` come with it and the probe checks
  the extensions are actually loaded rather than just that `php` exists.
  Composer is not reliably packaged at a useful version, so it comes from
  upstream — but as a **pinned release verified against its published
  sha256**, not piped into the interpreter. Bumping the version means
  bumping the digest alongside it. Adds a `getcomposer.org` egress
  dependency (#141).

- **`[sandbox] languages = ["java"]` provisions a JDK and Maven** (issue
  #161). apt for both, pinned to `openjdk-21-jdk` rather than floating on
  `default-jdk`, which moves between distro releases. `JAVA_HOME` is part of
  the contract — many build tools read it directly rather than looking for
  `java` on PATH — so the entry records it in `/etc/sandbox-persistent.sh`,
  which the worker already loads into the environment the agent session and
  its shell commands inherit. The value is derived from the installed
  `javac` rather than hardcoded, since the JVM directory name embeds the
  distro architecture. Gradle is deliberately not installed: the `gradlew`
  wrapper is the norm and fetches its own distribution, which makes it an
  egress question (#141) rather than a package. Accepted spellings: `java`,
  `jdk`, `jvm`.

- **`[sandbox] languages = ["ruby"]` provisions Ruby** (issue #158).
  `ruby-full`, `ruby-dev`, `bundler`, and `build-essential` — apt only, and
  no egress beyond the always-reachable baseline. The dev headers and
  compiler are deliberately not optional: gems with native extensions
  (nokogiri, pg, …) fail to build when only `ruby` is present, which is the
  usual way a half-installed Ruby shows up. `build-essential` is shared with
  the C/C++ entry and installs once, not twice. Projects pinning an exact
  Ruby the distro does not carry still need `rbenv`, which is out of scope
  here. Accepted spellings: `ruby`, `rb`.

- **`[sandbox] languages = ["cpp"]` provisions a C/C++ toolchain** (issue
  #170). `build-essential`, `cmake`, `ninja-build`, and `pkg-config` — pure
  apt, no installer, and no egress beyond the apt mirrors already in the
  always-reachable baseline, which makes this the cleanest entry in the
  Layer 1 set. Accepted spellings: `cpp`, `c`, `c++`, `cxx`, `c-cpp`. The
  probe checks `gcc`, `g++`, `make`, `cmake`, and `pkg-config`; `ninja` and
  `clang` are optional extras a build can do without, so a template that
  lacks them is not reinstalled over.

- **`[sandbox] languages` selects which language toolchains the agent
  sandbox is provisioned with** (issue #144, layer 1 of the language-bias
  investigation #140). `_ensure_dev_tools` apt-installed `python3-venv` and
  `python3-pip` for the *agent's* project before its first turn — a real
  head start, but one only Python got: a Node, Rust, or Ruby task
  discovered its missing compiler on first failure and spent revision
  budget bootstrapping it. That call is now one entry in a registry
  (`sbxloop.toolchains`) selected by config rather than a hardcoded special
  case. Semantics are the ones the 0.4.0 field failure taught us and they
  apply to every entry: probe first (a template that already ships the
  toolchain costs no apt call and no network), never fatal (a failure warns
  with the toolchain named and the run continues on the agent's own
  `sudo apt-get` escape hatch), and opt-in only. Selected apt packages are
  pooled into a single `update && install`, so N languages is one round
  trip. **Behavior is unchanged for existing runs**: unset means
  `["python"]`. Setting the key replaces that default rather than adding to
  it — this is the point, since no language should be privileged by
  accident of implementation. Python is the only registered entry so far;
  the other nine follow.

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
  JavaScript/Node, TypeScript, Go, Rust, Ruby, Java/JVM, C#/.NET, PHP,
  C/C++ — the full ten-language set Layer 3 tracks.

- **Package-registry egress levels up rather than down** (issues #141,
  #145). The network baseline privileged Python: `pypi.org` and
  `files.pythonhosted.org` were unconditionally reachable while every other
  language's registry cost a plan declaration the planner had to remember —
  and a forgotten declaration is a failed run, not a degraded one. #145
  settled the direction for the whole layer: promote the other registries to
  PyPI's tier rather than demote PyPI to theirs. Demotion could not have
  produced real parity anyway — the worker's own `pip install` runs at
  provision time, before a plan exists to declare egress in — and it would
  have broken every existing plan that never declared PyPI.

  Structurally, `policy.PROMPT_ADVERTISED_DOMAINS` is now the union of
  `BASELINE_REGISTRY_DOMAINS` (the language registry tier, starting with
  Python's two hosts) and `APT_MIRROR_DOMAINS` (language-neutral distro
  infrastructure, baseline regardless). `sbxloop config policy` prints the
  two tiers separately, so what is unconditionally reachable is legible
  without reading the source. Promoted into the registry tier so far:

  - Python — `pypi.org`, `files.pythonhosted.org` (#145)

  - JavaScript/Node — `registry.npmjs.org`, `registry.yarnpkg.com`, and
    `codeload.github.com` for `github:user/repo` dependencies, whose
    tarballs come from a different host than the clone (#148)

  - TypeScript — no new domains: the compiler and `@types/*` packages are
    plain npm packages, so the Node promotion covers the whole toolchain.
    A type-check-only task over vendored dependencies needs no egress at
    all, and an empty `egress` is a complete plan rather than a forgetful
    one (#151)

  - Go — `proxy.golang.org` and `sum.golang.org` together: a reachable
    proxy whose checksum database is blocked fails `go mod download` at
    verification, which reads as a broken toolchain rather than a policy
    decision (#154)

  - Java — `repo.maven.apache.org` and `repo1.maven.org` (Maven Central),
    plus `plugins.gradle.org` and `services.gradle.org`. Java was in
    *neither* tier: a plan could not even declare Central without operator
    configuration. Gradle needs the plugin portal and the wrapper
    distribution host as well — Central alone still fails the build (#162)

  - C#/.NET — `api.nuget.org` and `nuget.org`, also previously in neither
    tier. `dotnet restore` runs implicitly inside `dotnet build` and
    `dotnet test`, so an unreachable feed surfaced as a build failure
    rather than an install failure (#165)

  - PHP — `repo.packagist.org` and `packagist.org`, the third registry that
    was in neither tier. Composer also fetches many dist zips from
    `codeload.github.com`, already baseline since the Node promotion —
    without it, `composer install` fails only for the packages that happen
    to be served from GitHub (#168)

  - Ruby — `rubygems.org` and `index.rubygems.org` (bundler's compact index
    is a separate host). This was the case `policy.py` cited as motivating
    the declarable tier in the first place — "write a Rails app"
    bundle-installing out of the box — and it no longer depends on the plan
    remembering (#159)

  - Rust — `crates.io`, `static.crates.io`, and `index.crates.io`. Cargo
    resolves from the sparse index, downloads from static, and talks to the
    API separately; two of the three fails mid-resolution (#156). The
    `rustup` installer domains are toolchain rather than registry and stay
    with the Layer 1 work (#143)

  - C/C++ — no new baseline domains. Its default dependency source is apt,
    already baseline, and the apt-only path is now covered by tests: useful
    evidence that the baseline works when a language's dependencies come
    from it. Conan (`center.conan.io`) is declarable rather than seeded —
    a real registry, but not how C/C++ dependencies normally arrive — and
    vcpkg stays operator-configured, since it fetches source tarballs from
    whatever upstream each port names and no fixed host set can cover that
    (#171)

  The plan and execute prompts no longer hardcode the tiers: both lists are
  injected from `policy.py` at render time, so a promotion cannot leave the
  prompts telling planners to declare a domain that needs no declaration.

  `WELL_KNOWN_REGISTRY_DOMAINS` — the declarable-without-configuration tier —
  survives as the second-line case: a legitimate registry that is not how a
  language's dependencies normally arrive, so a plan names it and the grant
  is event-logged. Conan is its one member today. An empty tier is also a
  supported state: `sbxloop config policy` and the prompts both render it
  explicitly rather than printing a blank.

  The promotion trades some audit granularity — a baseline registry emits no
  `policy.allow` event, because there is no grant to log — so it is bounded
  to read-only public registries for supported languages.
  **`[policy] deny` now wins over the always-reachable tier too**: denied
  domains are filtered out of the set provisioning seeds
  (`policy.baseline_allows`) instead of being seeded and then refused a
  redundant re-grant. Previously a `deny` on `pypi.org` left it reachable,
  because provisioning seeded it before any grant could be refused.

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

- **An inner command saying "not found" no longer aborts the run.**
  `sbx exec` classified any stderr containing that substring as an
  sbx-level failure and raised, instead of returning the nonzero result
  its callers are written against. But `exec` is the one CLI call whose
  stderr belongs to somebody else's program, and "not found" is the single
  most common thing a shell says — `sh: dpkg: command not found`, curl's
  `404 Not Found`, npm's `404 Not Found - GET`. Every best-effort probe
  built on `result.ok` could therefore be turned into a hard failure by a
  missing binary: the dev-tools ensure, the search-fallback ensure, and the
  bake that runs them. A missing *sandbox* now has to say so (real sbx and
  the fake both name the sandbox in that message); the infra markers for a
  stopped VM or an unreachable daemon are unchanged.

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
