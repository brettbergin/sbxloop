# Changelog

All notable changes to sdxloop are documented here. The project adheres to
[Semantic Versioning](https://semver.org/) and both distributions (`sdxloop`,
`sdxloop-worker`) release in lockstep.

## [Unreleased]

## [0.1.10] — 2026-07-23

### Changed
- The live run view is now a chat-style transcript: agent messages render
  as markdown panels (fenced ```json blocks syntax-highlighted and
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
  python3-venv: it self-heals via `sudo apt-get install python3-venv
  python3-pip` and, failing that, falls back to a user-site pip install
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
