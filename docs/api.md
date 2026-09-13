# The remote API

The daemon's remote operations API: how to switch it on, register a client,
mint a token, and drive a run from admission to its result without a shell
on the host. This page is the operator's and integrator's reference; the
contract of record is the OpenAPI document the listener publishes at
`/v1/openapi.json` (the committed copy is [`openapi.json`](openapi.json)).
The design it implements is the [remote API spike](spikes/remote-api.md);
the internals are in [architecture.md](architecture.md#the-remote-api-listener).

- [Installation](#installation)
- [Clients and tokens](#clients-and-tokens)
- [Local collaboration](#local-collaboration)
- [Capability discovery](#capability-discovery)
- [Resources and ids](#resources-and-ids)
- [Endpoint catalog](#endpoint-catalog)
- [Commands, operations and idempotency](#commands-operations-and-idempotency)
- [Following the work](#following-the-work-events-sse-and-the-websocket)
- [Errors](#errors)
- [Limits](#limits)
- [Isolation](#isolation)
- [Recovery procedures](#recovery-procedures)
- [What is not offered](#what-is-not-offered)
- [Readiness criteria for a hosted service](#readiness-criteria-for-a-hosted-service)

## Installation

The API runs **inside `sbxloop daemon`**: one process owns execution, and the
listener is a second way in, never a second scheduler. Install the extra and
switch it on:

```bash
uv tool install 'sbxloop[api]'      # or: pip install 'sbxloop[api]'
```

```toml
# $SBXLOOP_HOME/config/sbxloop.toml
[api]
enabled = true
bind = "127.0.0.1"        # loopback by default; your reverse proxy terminates TLS
port = 8420
trusted_proxies = []      # the proxy's address, so client addresses are read from it
```

A daemon with `enabled = true` and the extra missing refuses to start and
names the extra. The listener speaks plain HTTP and never terminates TLS:
put a reverse proxy in front for anything beyond the host, and list it in
`trusted_proxies` ([deploy.md](deploy.md#the-remote-api-behind-a-proxy)).
Every `[api]` key is in the [user guide's knob table](user-guide.md#configuration).

`GET /health/live` answers as soon as the process is up; `GET /health/ready`
answers `503` until recovery has established execution ownership and the
daemon takes commands, then `200` with the daemon's `generation` and the
public chronology's lag.

## Local collaboration

The API can also serve a single-user local product such as Angie. This is an
additive layer around the existing run API: it uses the daemon's store,
concierge, operation controls, schedules, events, artifacts, and usage instead
of starting another scheduler or opening another SQLite writer.

The installation has at most one local user. `POST /v1/auth/local/register`
creates that profile and its scoped API principal; `/v1/auth/local/login` returns
the same short-lived access and rotating refresh tokens as the existing client
credential flow. Existing machine clients and all existing routes keep their
original behavior.

| Resource    | Routes                                           | Purpose                                                      |
| ----------- | ------------------------------------------------ | ------------------------------------------------------------ |
| Profile     | `GET/PATCH /v1/users/me`                         | Local identity and timezone                                  |
| Agents      | `GET /v1/agents[/{slug}]`                        | Native roles, backend, and configured models                 |
| Teams       | `/v1/teams[/{id}]`                               | Durable named groups of agent roles                          |
| Channels    | `/v1/channels[/{id}]`                            | Revisioned conversation containers; deletion tombstones them |
| Messages    | `GET /v1/channels/{id}/messages`                 | Immutable, monotonically sequenced history                   |
| Turns       | `POST /v1/channels/{id}/turns`, `GET .../{turn}` | Idempotent input acceptance and durable completion state     |
| Preferences | `/v1/prompts`, `/v1/prompts/definitions`         | Prompt context saved for the local user                      |
| Workflows   | `/v1/workflows[/{id}]`                           | Workflow metadata used by the Angie management screen        |
| Connections | `/v1/connections`, `/v1/connections/services`    | Redacted view of operator-managed sbxloop integrations       |

An ordinary turn talks to Angie without host or MCP action tools. A known
`@agent`, an enabled `@team`, explicit `target_slugs`, or `intent=delegate`
records work intent and enables the corresponding concierge tools. Team members
receive separate role-scoped sessions and their replies are persisted as
separate messages. Repeating a `client_turn_id` returns the accepted turn;
reusing it for different text, targets, intent, or message identity is rejected.

Discovery lists sbxloop's five native roles: `concierge`, `planner`, `builder`,
`critic`, and `operator`. Chat resolves their models through the existing
configuration, using the `concierge`, `decompose`, `build`, `review`, and
`operator_plan` phases respectively. `phase_models` also reports the remaining
engine phases for each role. The same refreshed model settings drive dispatch.
Legacy product role names remain addressable for existing saved teams and clients
but are excluded from discovery.

Native chat shares the concierge transport and its host-tool boundary. Actual
code changes and workload execution use managed runs; a chat session has no
checkout, editor, or shell. Critic chat receives only explicitly allowed read-only
host tools and no MCP servers. The host rejects tools outside that allowlist.

Delegated chat also exposes `handoff_agent(agent_slug, message)`. An agent can
ask any other native role for help; plain `@mentions` in its prose do not
dispatch work. The tool queues a peer response in the same turn after already
queued participants, without waiting inside the current response. The peer
receives the original user request, the scoped peer request, and prior chat
replies. It may explicitly hand back to the sender for synthesis.

Handoffs append durable `agent_handoff` messages and
`collaboration.handoff.queued` events. Per-member progress includes
`requested_by`, `parent_index`, `request`, and `read_only`. Handoff events and
concierge tool logs omit the peer request text. Repeating the same sender/recipient/request
within one response returns the original receipt without another invocation.
The user's original targets and idempotency identity remain unchanged.

Each response can request two peers, with six additional responses total and
three handoff levels per user turn. A role cannot hand off to itself. Critic
and every descendant of a read-only response retain the read-only host-tool
allowlist and no MCP servers, regardless of the recipient role. A peer request
does not grant new human approval. Ordinary conversation has no handoff tool.
Stop skips queued peers as well as team members. Restart recovery considers all
dynamic participants, and never replays an interrupted handoff.

`GET /v1/channels/{id}/turns?active_only=true` reports active turns and per-member
progress. `POST /v1/channels/{id}/turns/{turn}/cancel` stops queued responses.
An in-flight member may finish and its result is preserved; remaining members
are skipped. This does not undo effects or cancel previously dispatched runs.
Interrupted cancellation settles durably as cancelled on restart.

`GET /v1/events?latest=true&limit=200` reads the newest bounded snapshot, in
ascending sequence order, including after retention has pruned earlier events.
It cannot be combined with `after`; existing cursor replay and streams retain
their behavior. HTTP requests produce structured `api.request` logs with route
templates, status, generated trace IDs, and duration. Request bodies, query
strings, authorization headers, and caller-provided request IDs are not logged.

Turns execute in admission order through the single concierge. A team's members
run sequentially, each receiving durable channel history and the earlier members'
replies. The context is bounded to the latest 200 messages and 60,000 characters;
it excludes other channels and later queued user inputs. History also rebuilds
context after a provider session is lost or the daemon restarts.

Before serving requests after a restart, the API resumes accepted turns that
never started. A running turn with every expected reply already persisted is
completed without another invocation. Other interrupted running turns fail with
a durable explanation: their actions may already have occurred, so they are not
automatically replayed. Provider failures also appear in channel history.
Deleting a channel prevents queued turns and remaining team members from starting
and discards late replies. It does not undo an action already running.

Connection credentials remain in sbxloop's environment and configuration.
These routes report redacted readiness and deliberately reject browser-supplied
secret mutation until protected credential intake is implemented (#1043).

## Clients and tokens

sbxloop issues its own tokens. A client is registered on the host with the
capabilities it may hold; its secret is printed once:

```bash
sbxloop api client create ci-reporter --cap runs:read --cap audit:read
sbxloop api client create deployer --cap runs:read --cap daemon:manage
sbxloop api client list
sbxloop api client revoke cli_…
sbxloop api key rotate           # the signing key; the previous one stands until its tokens expire
```

A client exchanges its secret for a short-lived access token and a rotating
refresh token:

```bash
curl -s -X POST http://127.0.0.1:8420/v1/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"grant_type":"client_credentials","client_id":"cli_…","client_secret":"sk_…"}'
# {"token_type":"Bearer","access_token":"…","expires_in":900,"refresh_token":"rt_…","refresh_expires_in":604800}
curl -s -X POST http://127.0.0.1:8420/v1/auth/token \
  -d '{"grant_type":"refresh_token","refresh_token":"rt_…"}'
curl -s -X POST http://127.0.0.1:8420/v1/auth/revoke -H 'Authorization: Bearer …'
```

Rules a client can rely on:

- Bearer only: `Authorization: Bearer <access token>`, on every request and
  on the WebSocket's upgrade (or its first frame); never a cookie, never the
  query string.
- The access token is an Ed25519-signed JWT (`iss=sbxloop`, `aud=sbxloop-api`),
  valid for `[api] access_token_ttl_s` (15 minutes by default). Its
  capabilities are what the client held when it was minted **and still
  holds**: a grant narrowed later narrows the live token at once; a revoked
  client is refused on its next request and dropped from its streams.
- A refresh token is used once. Presenting it twice revokes its whole family
  (`401 refresh_reuse_detected`); the client re-authenticates with its secret.
- Authentication failures are rate-limited per client id and per source
  address (`429`, with `Retry-After`).

### Capabilities

| Capability               | Grants                                                                                      |
| ------------------------ | ------------------------------------------------------------------------------------------- |
| `runs:read`              | Every read: status, items, queue, runs, tasks, events, streams, gates, holds, schedules     |
| `items:create`           | `POST /v1/items`                                                                            |
| `runs:control`           | Cancel, resume, retry, requeue, abandon, re-arm the review wait                             |
| `runs:steer`             | `POST /v1/runs/{id}/steering`                                                               |
| `budgets:grant`          | `POST /v1/runs/{id}/round-grants`                                                           |
| `gates:approve`          | `POST /v1/gates/{id}/approve`                                                               |
| `daemon:manage`          | Holds, stop, restart, repository resume, schedules                                          |
| `artifacts:read`         | Artifact catalogs and downloads                                                             |
| `audit:read`             | `GET /v1/operations`                                                                        |
| `diagnostics:read`       | `GET /v1/logs`, `GET /v1/configuration`                                                     |
| `collaboration:read`     | Local profile, agent/team catalogs, channels, messages, preferences, workflows, connections |
| `collaboration:write`    | Local profile, teams, channels, preferences, and workflow mutations                         |
| `collaboration:delegate` | Accept a conversational or delegated channel turn                                           |

A refusal names the capability it needed (`403 forbidden` with
`"capability"`), before the target is looked at.

## Capability discovery

`GET /v1/capabilities` (any token) says what this installation serves:

```json
{
  "contract_version": 1,
  "server_version": "…",
  "workspace_id": "local",
  "features": ["status", "operations", "auth.client_credentials", "…", "schedules"],
  "capabilities": ["runs:read", "…"],
  "run_kinds": ["code", "workload", "tool"],
  "limits": {"page_default": 50, "page_max": 200, "max_body_bytes": 262144, "…": "…"},
  "retention": {"replay_s": 604800, "idempotency_s": 86400, "operation_deadline_s": 300}
}
```

`GET /v1/me` is the client as its token stands now. A client should read
`features` before relying on a route: a later release adds features; it does
not remove them within a contract version.

## Resources and ids

Every id is opaque and stable; none is an issue number, a host path or an
`owner/name`. `itm_…` a work item, `run_…` a run, `repo_…` a configured
repository, `gate_…` a merge or publication gate, `op_…` an operation,
`str_…` a steering record, `art_…` an artifact, `evt_<n>` an event (and the
cursor into the chronology), `cli_…` a client. An unknown id of any kind is
a plain `404 not_found`. Each resource carries `workspace_id` (`"local"` on
a single installation), RFC 3339 UTC timestamps, `available_actions` (what
the daemon would accept for it right now — advice for a UI; the command is
rechecked when it arrives) and a `revision` a command may pin.

## Endpoint catalog

| Method   | Path                                         | Capability             | Purpose                                                               |
| -------- | -------------------------------------------- | ---------------------- | --------------------------------------------------------------------- |
| `GET`    | `/health/live`, `/health/ready`              | none                   | Liveness; readiness with generation and projection lag                |
| `GET`    | `/v1/capabilities`, `/v1/me`                 | any                    | Contract, features, limits; the client's own grant                    |
| `GET`    | `/v1/openapi.json`                           | none                   | The contract of record                                                |
| `POST`   | `/v1/auth/token`, `/v1/auth/revoke`          | none / any             | Mint and refresh; revoke the presented token                          |
| `POST`   | `/v1/auth/local/register`, `/login`          | none                   | One local user's onboarding and login                                 |
| `GET`    | `/v1/users/me`, `/v1/agents[/{slug}]`        | collaboration read     | Local profile and product agent catalog                               |
| CRUD     | `/v1/teams`, `/v1/channels`, `/v1/workflows` | collaboration          | Local teams, durable conversations, and workflow definitions          |
| `GET`    | `/v1/channels/{id}/messages`                 | collaboration read     | Immutable ordered conversation history                                |
| `POST`   | `/v1/channels/{id}/turns`                    | collaboration delegate | Accept an idempotent conversation/delegation turn                     |
| CRUD     | `/v1/prompts`, `/v1/connections`             | collaboration          | User preferences; redacted operator-managed connection status         |
| `GET`    | `/v1/status`                                 | `runs:read`            | Live state: current run, queue, holds, breaker, stopping, watermark   |
| `GET`    | `/v1/items[/{id}]`, `/v1/queue`              | `runs:read`            | Work items; the queue in dispatch order                               |
| `POST`   | `/v1/items`                                  | `items:create`         | Admit an issue, a workload ask or a tool recipe                       |
| `POST`   | \`/v1/items/{id}/retry                       | requeue                | abandon\`                                                             |
| `GET`    | `/v1/runs[/{id}]`, `…/tasks`                 | `runs:read`            | Runs and their tasks                                                  |
| `POST`   | \`/v1/runs/{id}/cancel                       | resume\`               | `runs:control`                                                        |
| `POST`   | `/v1/runs/{id}/steering`                     | `runs:steer`           | Direction for the run in flight                                       |
| `GET`    | `/v1/runs/{id}/steering`                     | `runs:read`            | Every instruction and its fate                                        |
| `POST`   | `/v1/runs/{id}/round-grants`                 | `budgets:grant`        | More review rounds for an exhausted run                               |
| `POST`   | `/v1/runs/{id}/review-wait/resume`           | `runs:control`         | Re-arm a run parked for review                                        |
| `GET`    | `/v1/gates[/{id}]`                           | `runs:read`            | Merge and publication gates                                           |
| `POST`   | `/v1/gates/{id}/approve`                     | `gates:approve`        | Endorse and release a gate at a revision                              |
| `GET`    | `/v1/events`, `/v1/runs/{id}/events`         | `runs:read`            | The chronology after a cursor                                         |
| `GET`    | `/v1/events/stream`                          | `runs:read`            | The same, as server-sent events                                       |
| `WS`     | `/v1/ws`                                     | `runs:read`            | Events and commands on one socket                                     |
| `GET`    | `/v1/runs/{id}/artifacts`                    | `artifacts:read`       | The run's artifact catalog and where it published                     |
| `GET`    | `/v1/artifacts/{id}[/content]`               | `artifacts:read`       | One entry; its bytes as an attachment                                 |
| `GET`    | `/v1/runs/{id}/usage`, `/v1/usage`           | `runs:read`            | Reported tokens and turns; never a bill                               |
| `GET`    | `/v1/operations[/{id}]`                      | `audit:read`           | Every command any surface recorded                                    |
| `GET`    | `/v1/repositories`, `/profiles`, `/recipes`  | `runs:read`            | What work may be admitted against                                     |
| `POST`   | `/v1/repositories/{id}/resume`               | `daemon:manage`        | Poll a suspended repository again                                     |
| `GET`    | `/v1/daemon/holds`                           | `runs:read`            | Standing holds and whose they are                                     |
| `POST`   | `/v1/daemon/holds`                           | `daemon:manage`        | Take a hold attributed to this client                                 |
| `DELETE` | `/v1/daemon/holds/{name}`                    | `daemon:manage`        | Release your hold; `?force=true` overrides another's                  |
| `POST`   | `/v1/daemon/stop`, `/v1/daemon/restart`      | `daemon:manage`        | Graceful stop; a stop the supervisor undoes                           |
| `GET`    | `/v1/schedules[/{name}]`                     | `runs:read`            | Schedules with cadence, last and next due                             |
| `POST`   | \`/v1/schedules\[/{name}/pause               | resume\]\`             | `daemon:manage`                                                       |
| `DELETE` | `/v1/schedules/{name}`                       | `daemon:manage`        | Remove                                                                |
| `GET`    | `/v1/logs`, `/v1/configuration`              | `diagnostics:read`     | The log ring, redacted; the allowlisted configuration with provenance |

Every collection pages by an opaque `cursor` bound to its filters
(`limit` up to 200; `{"data": […], "next_cursor": …, "has_more": …}`). The
[user guide](user-guide.md#the-remote-api) walks each group with examples.

## Commands, operations and idempotency

Every mutation is one **operation**: accepted durably (its row, its
idempotency record and its audit event in one transaction) before it acts,
claimed under the daemon's generation, applied through the same service
`ctl` and chat use, finished with its outcome. The reply carries the
operation; `GET /v1/operations/{id}` is the record afterwards, and the
operation's transitions ride the chronology.

- **Idempotency.** Send `Idempotency-Key` on any mutation; `POST /v1/items`
  requires it. The key is scoped to the workspace, the client, the method and
  the route, so two clients' keys never collide. A replay with the same body
  answers with the operation that already exists (`200`, `created: false`);
  a different body under the same key is `409 idempotency_conflict`; a
  replay while the first attempt is still being applied is
  `409 already_in_progress`. Keys are kept for `[api] idempotency_retention_s`.
- **Revisions.** Pass `expected_revision` to act on exactly the state you
  read; a state that moved is `409 stale_revision`. Gate approval requires
  it.
- **Acceptance is not completion.** A cancel answers `202` and is honoured
  at the run's next boundary; a gate approval answers `202` and the landing
  completes afterwards (`gate.resolved` in the chronology); a stop or restart
  answers `202` the moment it is durably accepted, before the process exits.
  Watch the operation or the chronology for the effect.
- **Bots get one answer.** A command refused by policy is a recorded
  operation in state `failed` with its `error_code`; retrying it is a new
  operation, not a fix loop.

## Following the work: events, SSE and the WebSocket

The **chronology** is one durable, ordered stream: the daemon's notices, a
run's start and finish, its engine events (every persisted one, `worker.stdout`
included — filter with `type_prefix`), gate transitions, steering receipts,
and every operation any surface recorded. Each event's `id` (`evt_<n>`) is
also the cursor.

1. Read a snapshot: `GET /v1/status` reports `watermark`.
2. Read what came after it: `GET /v1/events?after=evt_<watermark>` — pages
   neither gap nor repeat.
3. Subscribe from the last id you saw: `GET /v1/events/stream` with
   `Last-Event-ID` (or `?after=`), or `subscribe{after}` on `/v1/ws`.
4. On a disconnect, reconnect from the last id. The stream says why it
   closed (`stream.closed` / `closing{reason}`: `daemon_stopping`,
   `client_revoked`, `token_expired`).

History is kept for `[api] replay_retention_s`; a cursor older than what
remains is `410 cursor_expired` with a pointer to the snapshot, never a
silent skip. The WebSocket takes the same typed commands as REST
(`command{id, action, target, params, idempotency_key, expected_revision}`,
answered by `reply{id, ok, result | problem}`) — `item.*`, `run.*`,
`gate.approve`, `daemon.*`, `repository.resume`, `schedule.*` — with the same
idempotency scope; a stop or restart sent on the socket takes effect after
its reply frame.

## Errors

Every refusal is `application/problem+json` with a stable `code`, the
request's `X-Request-Id`, and the fields a client needs to act:

| Status | Codes                                                                                                                                                                                                     |
| ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 400    | `invalid_request`, `invalid_cursor`                                                                                                                                                                       |
| 401    | `unauthenticated`, `invalid_token`, `token_expired`, `token_revoked`, `client_revoked`, `refresh_reuse_detected`                                                                                          |
| 403    | `forbidden` (with `capability`)                                                                                                                                                                           |
| 404    | `not_found`, `unknown_target`                                                                                                                                                                             |
| 409    | `not_eligible`, `already_terminal`, `already_in_progress`, `stale_revision`, `unsupported_for_kind`, `capability_unknown`, `capability_unsupported`, `idempotency_conflict`, `hold_owned`, `unsupervised` |
| 410    | `cursor_expired` (with `snapshot`), `artifact_gone`                                                                                                                                                       |
| 411    | `length_required`                                                                                                                                                                                         |
| 413    | `body_too_large` (with `limit`)                                                                                                                                                                           |
| 422    | `invalid_request` (with `errors`), `invalid_argument`, `idempotency_key_required`, `unknown_action`                                                                                                       |
| 429    | `too_many_attempts`, `too_many_streams`                                                                                                                                                                   |
| 500    | `internal_error` (never the exception's text)                                                                                                                                                             |
| 503    | `daemon_not_ready` (with `Retry-After`), `daemon_stopping`, `source_unavailable`                                                                                                                          |

`unknown_target` and `not_eligible` carry the daemon's own sentence in
`detail` — the same one `ctl` prints.

## Limits

| Limit                          | Value                                              |
| ------------------------------ | -------------------------------------------------- |
| Page size                      | 50 default, 200 maximum                            |
| Request body                   | `[api] max_body_bytes` (256 KiB)                   |
| Live streams (SSE + WebSocket) | `[api] max_stream_clients` (32)                    |
| Concurrent artifact downloads  | 4                                                  |
| Artifact catalog per run       | 2000 files                                         |
| Log tail                       | 500 records                                        |
| Usage window                   | 90 days                                            |
| Access token                   | `[api] access_token_ttl_s` (15 minutes)            |
| Refresh token                  | `[api] refresh_token_ttl_s` (7 days)               |
| Auth failures                  | 10 per minute per client and address, 60 s lockout |
| Operation deadline             | `[api] operation_deadline_s` (5 minutes)           |
| WebSocket auth frame           | 5 s                                                |
| Store calls in flight          | 8 (4 executor threads)                             |

A stream's whole state is a cursor: a slow client blocks nobody, and the
daemon's stores are never touched from the event loop.

## Isolation

A worker sandbox can never reach the daemon's API. Two facts hold it:

- No allowlist the provisioner hands `sbx policy allow network` names the
  host, its loopback, or the address the listener binds — whatever the
  backend, the toolchains or the registries — and `[sandbox] extra_allow_domains`
  refuses a bare address, a loopback name, a container runtime's host
  alias and `*` (`tests/unit/test_api_isolation.py`).
- `sbxloop doctor --deep` probes it live: the `api-host-unreachable`
  conformance probe connects from inside a scratch sandbox to the API's
  port on the guest's loopback and on its default gateway, and asks the
  network policy about both addresses. Anything but `unreachable` fails the
  drift gate on CI runners.

**Field-unverified:** the probe's verdict against a real sbx release is
established by the CI runners that execute `doctor --deep`, never assumed
from a developer machine.

## Recovery procedures

- **The daemon restarted.** Holds stand with their owners; the queue is
  intact; a run in flight resumes through the daemon's own queue. Read
  `/health/ready` for the new `generation`, then `GET /v1/status` and
  subscribe from its watermark. Operations a previous generation left open
  are settled from evidence (`succeeded`, `failed interrupted_before_effect`,
  or `expired` for intent never claimed) — read them, never retry blindly.
- **A reply was lost.** Replay the request with the same `Idempotency-Key`:
  the answer is the operation that already exists, whatever became of it.
- **A stream dropped.** Reconnect from the last `evt_<n>` you processed.
  On `410 cursor_expired`, read a fresh snapshot and subscribe from its
  watermark; what you missed is in the resources themselves.
- **A token stopped working.** `401 token_expired`: refresh. `401 client_revoked`: the operator revoked the client; work already admitted
  stands. `401 refresh_reuse_detected`: the family was revoked; mint from
  the secret and treat the reuse as a leak.
- **A hold you did not take blocks the queue.** `GET /v1/daemon/holds`
  names its owner; release it with `?force=true` only as an override, which
  the record shows as yours.
- **The daemon is down.** `GET /health/live` refuses the connection;
  starting it is the supervisor's job, not the API's. A `POST /v1/daemon/stop`
  under a service manager that restarts the daemon is a restart, holds
  included.
- **Something disagrees with the record.** `GET /v1/operations` is the
  record every surface writes; `GET /v1/logs` and `GET /v1/configuration`
  say what the daemon runs on, with secrets and host paths redacted.

## What is not offered

By design, on this API: configuration writes, repository registration,
backup and restore, garbage collection, sandbox deletion, and starting a
daemon that is not running. Each stays on the host's own CLI until it has
its own attribution, conflict and active-run story. A tool run takes no
steering, no round grants and no gate: a fixed recipe has nothing to steer.

## Readiness criteria for a hosted service

The single-installation loop is complete when every scenario in
`tests/api/conformance/` passes on CI, the OpenAPI snapshot is committed
and unchanged by the build, and `doctor --deep` reports `api-host-unreachable`
as `unreachable` on a CI runner. Proceeding to a hosted, multi-tenant service
(the spike's stages four and five) additionally requires, before any
customer data enters:

1. **Identity.** A hosted identity provider behind the same capability
   model; `workspace_id` varying per tenant with every id, cursor, nested
   reference and download checked against it (today `"local"` everywhere).
2. **Execution routing.** One daemon per tenant home, or a scheduler that
   proves tenant isolation of sandboxes, stores and secrets — never a shared
   store.
3. **Credentials.** BYOK bindings per provider and backend with observed
   billing attribution; secrets never in events, logs or argv, as here.
4. **Durability.** A backup, restore and retention policy for the operation
   record and the chronology, with the upgrade and rollback limits published
   before two client versions read one store.
5. **Operations.** Request latency and error rates, pending-operation age,
   reconciliation backlog, projection lag and disconnected-consumer counts
   exported from the daemon; the limits above enforced per tenant.
6. **Field verification.** The isolation probe and the listener's restart
   behaviour driven on the target platform, and every remaining
   field-unverified note in the stack's pull requests closed.
