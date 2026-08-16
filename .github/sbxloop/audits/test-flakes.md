______________________________________________________________________

## every: 7d

# Audit: test flakes and timing assumptions

Read `tests/` for tests that depend on wall-clock timing, sleeps, thread
scheduling, ANSI/terminal state (`FORCE_COLOR`, `TERM`), the host's
environment (`COPILOT_GITHUB_TOKEN`, login-shell exports), the machine's
CPU count, or ordering between xdist workers. The recent flakes were
`test_heartbeat_thread` (a 4x timing margin) and help-text asserts under
rich colouring — look for the same shapes.

For each: Evidence (the assertion and what it assumes, file:line), how it
would flake (Repro: the env/timing that breaks it), a Proposal (poll instead
of sleep, strip ANSI, isolate env, widen the margin), Size small, Kind test.
