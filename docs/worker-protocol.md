# The host ↔ worker protocol

The host orchestrator and the in-sandbox worker communicate through files and
process streams — no sockets, no servers. All wire models live in
`sbxloop_worker.protocol` (pydantic, `extra="forbid"`, versioned `v: 1`);
the host imports the exact same module, so drift is a validation error.

## Filesystem layout (inside each sandbox)

```
/home/agent/.sbxloop/
  jobs/<job_id>.json      # JobRequest, written by the host (sbx cp)
  events/<job_id>.jsonl   # Event stream, appended by the worker (fsync/line)
  results/<job_id>.json   # JobResult, written by the worker — AUTHORITATIVE
  env.sh                  # plain-env secret strategy only
  venv/                   # worker virtualenv (created at provision time)
```

## Job lifecycle

1. Host writes `jobs/<id>.json` and runs
   `venv/bin/python -m sbxloop_worker run --job … --events … --result …`
   via `sbx exec`.
2. Worker emits `worker.start`, dispatches the job, emits events as it goes
   (mirrored to stdout for live streaming), writes `results/<id>.json`,
   emits `worker.result`/`worker.error` and `worker.end`.
3. Host fetches the result file with `sbx cp` and validates it. **The result
   file is the outcome; the event stream is telemetry** — either channel can
   be lost without corrupting a run.

Worker exit codes: `0` result written (including error/timeout results),
`64` usage error, `70` catastrophic (no result produced).

## Job kinds

| kind            | fields                                                                                                                | result                                                                                                                                                                         |
| --------------- | --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `agent.session` | `prompt`, `system_message?`, `model?`, `resume_session_id?`, `permission_mode: auto\|read_only`, `expect: text\|json` | `output_text`, `output_json` (extracted from the last \`\`\`json fence when `expect=json`; missing JSON ⇒ typed `ExpectedJsonMissing` error), `session_id`, `usage`, `health?` |
| `shell.check`   | `argv`, `cwd?`                                                                                                        | `exit_code` + captured output. A nonzero exit is an **ok** result — the host owns the verification decision                                                                    |
| `shell.batch`   | `commands`, `command_timeout_s?`, `cwd?`                                                                              | `output_json`: list of `{command, exit_code, output}` (one per command, in order); job `exit_code` is the first nonzero. Nonzero exits are still **ok** results                |
| `github.op`     | `op`, `params`                                                                                                        | op-specific JSON (see below)                                                                                                                                                   |

`permission_mode="auto"` approves every Copilot SDK permission request — the
microVM is the security boundary. `read_only` allows only `read`, `url`, and
`shell` requests (everything else, including unknown kinds, fails closed)
and is used for critic sessions.

`health` is a `SessionHealth` tally of what the session lost while it ran:
`permission_denials`, `tool_failures`, and `tool_refusals`, each a
`kind/tool → count` map (`null` when nothing was denied, refused, or
failed). Each denial also emits an `agent.permission_denied` event. The
engine's degraded-critic guard (#123) reads it: a critic `pass`/`accept`
from a session with failed tool calls is re-run once and, if still
degraded, downgraded. Denials never count as degradation (a read-only
critic probing `write` is the allowlist working as designed), and neither
do refusals — completions whose error/output starts with `Command not executed.`, the Copilot CLI's own validator declining to run a command
(e.g. `kill` without a literal numeric PID); the agent can rephrase and
retry, so nothing was lost.

`shell.batch` exists because every job pays a fixed round-trip cost (stage
the job JSON, boot a cold Python under `sbx exec`, fetch the result) that
dwarfs a mechanical command's real work: the host batches all verify
commands into one job, and the scrutinizer's evidence commands into
another. Each command runs via `sh -c` sequentially in the job's `cwd` with
`command_timeout_s` (default `timeout_s`) as its individual cap; `timeout_s`
bounds the whole job.

## Transports

- **stream** (default): one blocking `sbx exec` per job; the host parses
  stdout line-by-line onto its EventBus. Unparseable lines become
  `worker.stdout` events, never crashes.
- **poll** (`worker_transport = "poll"`): the worker is launched detached
  (`nohup … &`); the host tails `events/<id>.jsonl` by byte offset. Fallback
  for environments where long exec streams are unreliable.

Host-side timeout is `job.timeout_s` plus a grace period; on expiry the host
pkills the worker (pattern scoped to the job id) and raises
`WorkerTimeoutError`.

## Events

Envelope: `{v, ts, run_id, job_id?, type, data}` — one JSON object per line.

- Worker: `worker.start|heartbeat|result|error|end`, `agent.message`,
  `agent.message_delta`, `agent.tool_start|tool_end`, `agent.usage`,
  `agent.permission_denied`, `gh.op_start|op_end`,
  `sandbox.resources|resources_warning`
- Host: `run.start|state|end`, `task.start|state|end`, `phase.start|end`,
  `sandbox.provision_start|ready|cleanup`, `worker.stdout`

### Resource telemetry

`sandbox.resources` is emitted once at job start and then on every heartbeat:
disk usage of the workspace filesystem (statvfs), memory from
`/proc/meminfo`, 1-minute load average, plus a guardrail `level`
(`ok`/`warn`/`abort`) classified against the `--disk-warn`/`--disk-abort`/
`--mem-warn`/`--mem-abort` thresholds the host passes from `[limits]`. The
host enriches the event with the sandbox `role`. Escalations (ok→warn,
→abort) emit an additional edge-triggered `sandbox.resources_warning`. When a
job fails after crossing `disk_abort` or `mem_abort`, the worker rewrites the
result error to `SandboxResourcesExhausted` so a full disk or an OOM is
diagnosed instead of surfacing as whatever confusing error the in-VM tooling
produced. Query history with
`sbxloop logs <run> --type-prefix sandbox.resources`.

## GitHub ops

`github.op` jobs execute in the github sandbox only (the sole holder of
`GH_TOKEN`). Ops: `issue.create`, `issue.comment`, `pr.create`, `pr.comment`,
`contents.read`, `status.create`, `repo.get`, `ref.get`, `search.issues`,
`raw.api`, `blobs.create_many`. Transport inside the sandbox: `gh api` when gh
is available, otherwise a pure-stdlib REST client — both produce identical
result shapes.

Probes ask questions and get "no" as data: `repo.get` and `ref.get` accept
`allow_missing: true`, under which an expected miss (404; for `ref.get` also
the 409 GitHub returns for an empty repository) is an **ok** result of
`{"missing": true, "http_status": N}` rather than a failed job — so the
transcript shows no error event for a question whose answer was "no".

## Worker installation

At provision time the host resolves a worker wheel — vendored inside the
sbxloop package → built from a workspace checkout → PyPI at the exact
lockstep version — copies it into the sandbox, creates `~/.sbxloop/venv`,
installs it (`[copilot]` extra in the agent sandbox only), and verifies the
imported version matches the host exactly.
