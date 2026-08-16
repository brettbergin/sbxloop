______________________________________________________________________

## every: 7d

# Audit: verify-lint vs. the prompts

The plan/decompose prompts describe what a verify command may look like
(`packages/sbxloop/src/sbxloop/engine/prompts/{plan,decompose,execute}.md`)
and `packages/sbxloop/src/sbxloop/verifylint.py` mechanically rejects some
shapes. Find **gaps between the two**: a command shape the prompts allow (or
fail to warn about) that would fail or mislead under the runner's `sh -c`
(nested shells, bashisms, `$` expansions in double quotes, mutating commands,
bare tool names in the wrong ecosystem, `cd` into a subdirectory that verify
then forgets, `uv run` vs `.venv/bin` inconsistencies), or a lint rule the
prompts contradict. Read the lint tests too (`tests/unit/test_verifylint.py`).

For each real gap: Evidence (the prompt line and the lint rule, file:line), a
one-command Repro that passes the lint but fails or lies at runtime, a
Proposal (rule + remedy text, or a prompt line), Size, Kind. Nothing
speculative — if you cannot write the failing command, it is not a finding.
