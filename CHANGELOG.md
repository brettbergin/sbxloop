# Changelog

All notable changes to sdxloop are documented here. The project adheres to
[Semantic Versioning](https://semver.org/) and both distributions (`sdxloop`,
`sdxloop-worker`) release in lockstep.

## [Unreleased]

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
