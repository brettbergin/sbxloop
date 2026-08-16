______________________________________________________________________

## every: 7d

# Audit: daemon guardrails

`packages/sbxloop/src/sbxloop/daemon/loop.py`, `sources.py`, `store.py`,
`control.py`. Look for **paths that bypass a guardrail**: the circuit
breaker, the daily cap, pause, retry backoff, the attempt cap (attempts vs
resumes), the comment-lock claim, the post-mortem/audit caps, and operator
overrides (`abandon`/`retry`/`requeue`) — code that dispatches or files
without going through the same gate `tick()` uses, state that is not
persisted across a restart, or two code paths that can both act on one item.

Cite file:line for the gate and for the path around it; describe the
sequence that gets past it (a Repro is a unit test sketch using the fakes in
`tests/unit/test_daemon_loop.py`); propose the smallest fix. Prefer one
strong finding over five weak ones.
