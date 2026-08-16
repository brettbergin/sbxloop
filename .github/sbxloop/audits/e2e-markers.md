______________________________________________________________________

## every: 14d

# Audit: e2e markers and unverified paths

`tests/unit/test_e2e_markers.py` audits `TODO(e2e #issue)` markers. List
every marker in `packages/**/*.py`, and for each decide: is the referenced
issue still open, does the code path have a unit test at least, and could it
be exercised cheaply (a fake, a fixture, a `sbxloop doctor --deep` probe)?
Also look for **untested branches** that talk to GitHub or sbx (grep for
`ops.raw(`, `self.cli.run(`) with no test that reaches them.

Findings: one per marker/path that can realistically be closed or covered,
with the concrete test or probe you would add (Kind: test). Do not file
"write more tests" in the abstract.
