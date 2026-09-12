<!--
Title in the imperative. One concern per PR; a stack for a campaign.

Fill in every section. Delete a checklist line only when it does not apply
to this change — an unticked box that does apply is a PR that is not done.
-->

## What broke

What a *target* repository hit: the symptom a run showed, or what the loop
could not do for a repository that is not this one. Name the repository
shape it came from (protected base, service-backed suite, a toolchain, a
private registry) rather than the internal detail you started from.

## What changed

The shape of the change and the decision behind it — which surfaces moved
(daemon, engine, prompts, `vcs/`, `sbx/`, config, worker) and what a run
does differently now. Say what you deliberately left out.

## How it was verified

Paste what you ran, not an adjective. Behaviour changes name the test that
failed before the change and passes after it.

- [ ] `uv run ruff format .` then `uv run ruff check --fix .`
- [ ] `uv run mdformat <touched .md files>` (never the example toml)
- [ ] `make lint`
- [ ] `uv run mypy`
- [ ] `make security`
- [ ] `make test-fast` on every commit; `make test` or CI — never a
  hand-picked subset — before merge
- [ ] A test that failed first covers each behaviour change

## What this change owes

- [ ] **Config key** — the model, the example config
  (`packages/sbxloop/src/sbxloop/data/sbxloop.toml.example`), the knob table
  in `docs/user-guide.md`, and `tests/unit/test_examples.py`; a per-repo
  override wherever `RepoConfig` already narrows
- [ ] **Toolchain or gate detection** — a fixture under
  `tests/fixtures/ecosystems/` and a row in `tests/unit/test_ecosystems.py`
- [ ] **Prompts** — domain-neutral (no language-specific examples in neutral
  rules, no incidents from this repository, no bare issue numbers from this
  tracker); `build.md`'s per-language parity test green
- [ ] **GitHub behaviour** — `tests/fakes/fake_github.py` extended in this PR
  for any shape it lacked; the ops layer never stubbed around it
- [ ] **Run kinds** — nothing assumes the task ends in code; the `code` run
  trail in `tests/unit/test_code_run_trail.py` is byte-identical
- [ ] **Secrets** — names travel, values ride the env-file path; nothing new
  in events, logs or `sbx` argv
- [ ] **Docs** — `docs/architecture.md` and `docs/user-guide.md` match the
  behaviour that ships

## Field-unverified

Claims about an external system that could not be verified in this session —
sandbox behaviour off a CI runner, a forge response only a live token sees,
a registry or toolchain not in the fixtures. List them, or write "none".

Closes #
