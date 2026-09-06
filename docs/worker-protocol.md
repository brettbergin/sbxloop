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
  env.sh                  # env-file delivery tier only (see below)
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

## Credentials

The worker takes credentials from its process environment first; the
`--env-file` (`~/.sbxloop/env.sh`) is loaded underneath it (existing env
wins, except over an sbx proxy sentinel). How they get there depends on the
delivery tier provisioning chose (see docs/architecture.md):

- **sbx secret proxy**: values never enter the VM at all.
- **per-job stdin** (#592): the host pipes `export KEY=VALUE` lines into
  the launch's stdin; the launch shell captures them into
  `SBXLOOP_JOB_ENV` and the login shell evals them after its profile ran,
  so the worker inherits them in memory — nothing at rest, nothing on any
  argv, and no env.sh needed.
- **env file**: the pre-#592 fallback; the worker loads it at startup.

## Job kinds

| kind            | fields                                                                                                                                       | result                                                                                                                                                                         |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `agent.session` | `prompt`, `system_message?`, `system_preset: true`, `model?`, `resume_session_id?`, `permission_mode: auto\|read_only`, `expect: text\|json` | `output_text`, `output_json` (extracted from the last \`\`\`json fence when `expect=json`; missing JSON ⇒ typed `ExpectedJsonMissing` error), `session_id`, `usage`, `health?` |
| `shell.check`   | `argv`, `cwd?`                                                                                                                               | `exit_code` + captured output. A nonzero exit is an **ok** result — the host owns the verification decision                                                                    |
| `shell.batch`   | `commands`, `command_timeout_s?`, `cwd?`                                                                                                     | `output_json`: list of `{command, exit_code, output}` (one per command, in order); job `exit_code` is the first nonzero. Nonzero exits are still **ok** results                |
| `github.op`     | `op`, `params`                                                                                                                               | op-specific JSON (see below)                                                                                                                                                   |
| `service.http`  | `params: {credential, method, path, query?, headers?, body?, timeout_s?}`                                                                    | `{credential, method, path, status, headers, body (clipped, credential value redacted), truncated, elapsed_s}` — see "Service ops"                                             |
| `service.fetch` | `argv`, `cwd`, `params: {ecosystem, verb, scrub_env}`                                                                                        | `exit_code` + captured output with every `scrub_env` variable's value blanked — see "Service ops"; a nonzero exit is an **ok** result                                          |

The `git.merge` job runs only on the agent worker. It requires `cwd` and
`params: {base_branch, base_sha, bundle_path?}` and returns
`{merged, conflicts, message}`. The host stages an optional Git bundle;
the job receives no remote URL or credential and needs no authenticated
network fetch. Repository hooks and drivers execute only in the agent VM.

`system_preset` (default `true`) keeps the backend's own coding-agent system
prompt under `system_message`; a workload's operator and judge sessions send
`false`, so an operator does not present as a coding agent and a judge does
not present as an operator. The Claude backend then passes the message alone
in place of its `claude_code` preset; the Copilot backend's SDK takes a system
message in `append` mode only, so the flag has no effect there —
the operator prompts are written to hold up under a coding-agent preamble.

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

## Host tools (agent.session)

An `agent.session` job may carry `host_tools: [HostToolSpec]` — tools the
**host** implements (daemon control, run inspection, work enqueueing) that
the in-sandbox session can call. The worker registers each as a custom SDK
tool; when the model invokes one:

1. The worker emits `agent.tool_request` (data = `HostToolCall`:
   `call_id`, `name`, `arguments`) on its event stream, which the host is
   already tailing.
2. The host runs the tool and copies a `HostToolResponse` JSON
   (`{v, call_id, ok, text, error?}`) to `<host_tools_dir>/<call_id>.json`
   inside the sandbox — `host_tools_dir` is set by the host
   (`~/.sbxloop/tools/<job_id>`, also passed as `--tools-dir` on the worker
   argv like `--cwd`), never derived by the worker.
3. The worker polls for the file (`sbx cp` is not atomic: a file that does
   not yet validate is still being written) and hands `text` back to the
   model; `ok=false` becomes a failed tool result the model can adapt to.
   `agent.tool_response` records the outcome (`ok`, `elapsed_s`, `error`).

`host_tool_timeout_s` (default 120) bounds one call; on expiry the session
sees a timeout result. `available_tools` restricts the SDK's built-in tools
(`[]` = host tools only; host tool names are always allowed). The echo
backend honours `host_tool_calls` in its script so the round trip is
testable without the SDK.

## Transports

Both transports are host-initiated by construction: the "no sockets, no servers"
rule above is a security decision, not an implementation convenience. See
[Design principles](architecture.md#design-principles) in the architecture doc —
new transports must be evaluated against host-initiated directionality and host
mediation between the sandboxes before anything else.

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
  `service.http_start|http_end`, `sandbox.resources|resources_warning`
- Host: `run.start|state|end`, `task.start|state|end`, `phase.start|end`,
  `sandbox.provision_start|ready|cleanup`, `service.call`, `worker.stdout`

### Tool calls

`agent.tool_start` carries `{tool, tool_call_id, args}`. `agent.tool_end`
carries `{tool_call_id, tool, args, success, exit_code, output, error, output_lines, duration_ms}` and is correlated to its start by
`tool_call_id` — parallel calls complete out of order, so consumers must
pair on the id rather than on command text.

- `output` is a bounded head+tail *excerpt* of the tool's combined output:
  the first `TOOL_OUTPUT_HEAD_LINES` (20) and last `TOOL_OUTPUT_TAIL_LINES`
  (20) lines, separated by an explicit `… N lines elided …` marker naming
  the omitted line count. The `TOOL_OUTPUT_CLIP` (1000) char cap is enforced
  structurally — each line is middle-elided at `TOOL_OUTPUT_LINE_CLIP` (200)
  and whole lines are then dropped from around the marker — so the first
  line, the marker and the last line survive however wide the output is,
  and an excerpt can never approach Discord's 2000-char message limit. The excerpt (and `error`) are secret-redacted inside the worker
  (`sbxloop_worker.secrets.redact_secrets`) *before* emission, so no
  credential-shaped text leaves the sandbox in an event.
- `output_lines` (int, optional) — total line count of the *untruncated*
  output, so a reader can see how much was elided.
- `duration_ms` (int, optional) — wall time from the matching
  `agent.tool_start`, measured in the worker.

**Compatibility.** `output_lines` and `duration_ms` are purely additive; no
existing field was removed or renamed. Older workers simply omit them and
host consumers (`run_events`, chronology, checkpoint/resume) treat them as
absent, so mixed worker/host versions interoperate in both directions.

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
`contents.read`, `status.create`, `repo.get`, `ref.get`, `label.get`,
`search.issues`,
`raw.api`, `blobs.create_many`, `checks.failed_logs` (the failing check runs on a
commit with their Actions job logs, head+tail clipped; the REST transport fetches
the log's blob-storage redirect without the bearer token), `token.scopes` (the
classic PAT's `X-OAuth-Scopes` from `GET /rate_limit`, or `null` for a
fine-grained PAT or App token — how `sbxloop doctor` learns what the credential
may do, #696). Transport inside the sandbox: `gh api` when gh
is available, otherwise a pure-stdlib REST client — both produce identical
result shapes.

Probes ask questions and get "no" as data: `repo.get`, `ref.get` and
`label.get` accept `allow_missing: true`, under which an expected miss (404;
for `ref.get` also the 409 GitHub returns for an empty repository) is an **ok**
result of
`{"missing": true, "http_status": N}` rather than a failed job — so the
transcript shows no error event for a question whose answer was "no".

## Service ops

`service.http` jobs execute in the service sandbox only (#765) — the box a
run granted `[[credentials]]` gets, holding exactly those values. The job
names a credential; the worker resolves it against the catalogue the host
put in `SBXLOOP_SERVICE_CREDENTIALS` (a JSON list of `{name, env, host, header, scheme}` — no values) and sends one request to
`https://<host><path>` with `<header>: <scheme> <value>` attached. The job
carries no host, so neither a model nor a mis-built job can point a
credential elsewhere. A header the op owns (`Host`, `Authorization`, the
credential's own header, `Content-Length`) is refused; redirects are not
followed (a 3xx is returned as its status with `location`); a non-2xx answer
is an **ok** result carrying that status — the host decides. The response
body is clipped head+tail and the credential's value replaced with `***`
wherever an API echoed it; request headers are never returned. Events:
`service.http_start {credential, method, path}` and `service.http_end {status, elapsed_s}`.

`service.fetch` jobs execute in the same box for a run whose repository has a
credentialed `[[registries]]` entry (#766): the package manager's argv exactly
as the host composed it from the ecosystem's fixed recipe (`npm ci --ignore-scripts`, `pip download -d <cache>`, `go mod download`, …), run in
the workspace mount the box shares with the agent box — never a shell, never
an argv the model wrote. `params.ecosystem` and `params.verb` name the fetch
for the events; `params.scrub_env` lists the registry credential variables
whose values the worker blanks out of the output before it leaves the box.
Events: `service.fetch_start {ecosystem, verb}` and `service.fetch_end {exit_code, elapsed_s}`.

On the host, the build session's `call_service` host tool is answered by
`ServiceOps`: the credential name is checked against the run's grant before
any job is built, and each call is one `service.call` host event
(`credential`, `method`, `path`, `status` or `error`, `duration_s`); its
`fetch_dependencies` tool the same way, each fetch — refused on the host or
run — one `sandbox.fetch` event (`ecosystem`, `verb`, `argv`, `exit_code`).

`SBXLOOP_SERVICE_FAKE=<path>` (tests) swaps the HTTPS transport for scripted
responses from that JSON file, each request appended to
`<path>.requests.jsonl`.

## Worker installation

At provision time the host resolves a worker wheel — vendored inside the
sbxloop package → built from a workspace checkout → PyPI at the exact
lockstep version — copies it into the sandbox, creates `~/.sbxloop/venv`,
installs it (`[copilot]` extra in the agent sandbox only), and verifies the
imported version matches the host exactly.
