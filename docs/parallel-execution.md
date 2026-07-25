# Parallel task execution across agent sandboxes

Status: MVP shipped (issue #53). Default behavior is unchanged — a run is
strictly sequential unless `[run] max_parallel > 1`.

## What it does

DECOMPOSE already produces a dependency DAG. With

```toml
[run]
max_parallel = 3   # or: sbxloop run "..." --max-parallel 3
```

the engine schedules that DAG in **waves**: each round it takes the tasks
whose dependencies are all done and packs a wave of tasks that may safely
run concurrently. Each task in a multi-task wave gets **its own agent
sandbox** — the isolation primitive that makes parallelism safe — and runs
its full per-task pipeline (PLAN → EXECUTE → SCRUTINIZE → VERIFY →
VALIDATE) there. `max_parallel = 1` (the default) never enters the wave
scheduler; the sequential loop is byte-for-byte the previous behavior.

## Ownership: the parallelism contract

The DECOMPOSE schema gained an optional per-task `owns` field: a list of
relative workspace paths (files or subtrees) the task claims exclusive
write access to. The prompt asks for it only when `max_parallel > 1`.

Two rules make a wave:

1. **Packing (plan time):** a task joins a multi-task wave only if it
   declares `owns` disjoint from every other task in the wave. A task with
   no `owns` always runs alone — undeclared writes cannot be scoped in
   advance, so it keeps sequential semantics.
2. **Enforcement (harvest time):** after the wave, each task's actual
   change set is computed exactly (see below). A completed task whose
   writes escape its declared `owns` is **failed loudly** — a
   `task.conflict` event names the offending paths, the feedback is
   recorded on the task, and *all* of its writes are discarded. Never
   last-writer-wins.

Because survivors' changes are subsets of pairwise-disjoint `owns`, the
final merge is conflict-free by construction. This is option (a) of the
issue (per-task subtree ownership) with option (b)'s staging directories
as the attribution mechanism.

## Workspace strategy

Whether multiple sandboxes can mount the same host directory — and with
what consistency — is an unverified sbx field question (option (c) in the
issue). The MVP does not depend on it:

- Before a multi-task wave, the engine snapshots the run's **merged tree**
  (the mounted workspace, or `runs/<id>/artifacts` in harvest mode),
  builds a seed copy, and `sbx cp`s it into a **freshly reset, isolated
  in-VM workdir** (`/home/agent/work`) in every participating sandbox —
  including the primary, even when the workspace mount was discovered.
  Parallel tasks never share a writable mount.
- After the wave, each workdir is harvested to
  `runs/<id>/staging/<task>` and diffed against the pre-wave baseline.
  One writer per sandbox makes attribution exact, including deletions.
- Surviving change sets are applied to the merged tree; a wave in a
  mounted run therefore lands its results in the live workspace just like
  sequential runs do.
- Single-task waves run exactly like the sequential loop (in the mount
  when there is one). In harvest mode the primary workdir is re-seeded
  first so a dependent task sees changes merged from sibling sandboxes.

**Hidden paths never propagate through a wave.** Snapshots, seeds, and
merges skip any path with a dot-component (mirroring `artifact_files`
semantics) — which also guarantees a mounted host workspace's `.git` can
never be modified or deleted by a merge. Tasks that need VCS state inside
their sandbox must create it themselves.

## Failure and resume semantics

Unchanged in kind: budget exhaustion or an ownership violation fails the
*task*; dependents are skipped; the run continues and finishes `failed`.
Infrastructure errors from any slot propagate after the wave's surviving
work is merged (partial artifacts beat none) and kill the run; `resume`
re-provisions and re-runs any task whose phases were never committed.
Extra sandboxes are cattle exactly like the pair. One known window: a
task that commits `done` but crashes the host before its wave merge loses
its staged changes on resume (the task will not re-run). Checkpointing
merge state per wave is follow-up work.

`SCRUTINIZE`/`VERIFY`/`VALIDATE` currently run per task, inside the
task's own sandbox, against that task's consistent tree — before the
merge. The issue sketches global gates over the merged result; a
post-merge re-verification pass is deliberately deferred (it needs a
seeded sandbox per wave and re-run of every task's verify commands) and
is tracked as follow-up. The conflict detection at merge time is the
gate that is genuinely global.

## Secrets for extra sandboxes

sbx keys custom secrets **globally by env name** (field-verified
2026-07-23): a second `secret set-custom` for `COPILOT_GITHUB_TOKEN`
would steal the binding from the primary sandbox. Extra agent sandboxes
therefore always receive the token via the in-VM env file (plain-env) —
the same fallback every exec-driven run already takes because proxy
secrets never reach `sbx exec` processes. Network policy is identical to
the primary agent sandbox.

## Cost guardrails

Every parallel slot is a full microVM **plus** a worker install (venv,
wheel, dev tools) — tens of seconds each today. Slots are provisioned
lazily on the first multi-task wave, so a graph that turns out sequential
never pays for them, but wall-clock wins below ~4+ meaningfully sized
independent tasks are unlikely until:

- warm sandbox pools (#47) absorb provisioning latency, and
- prebaked templates (#48) absorb the worker install.

Treat those as near-prerequisites for `max_parallel > 2` in practice.

## Open field questions

- Can multiple sandboxes mount one host dir concurrently (would remove
  the seed/harvest copies)? Unverified; the MVP deliberately avoids it.
- Does `sbx cp <host>/. box:dir` copy directory contents symmetrically to
  the harvest direction? Assumed (docker-style); e2e should assert it.
- Do Copilot session rate limits make N concurrent sessions queue at the
  API, erasing the win? Needs a real-sbx experiment.
- Whether revision-heavy waves thrash the seed/harvest path enough to
  justify rsync-style incremental copies.
