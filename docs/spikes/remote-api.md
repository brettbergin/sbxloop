# Expose sbxloop through a remote operations and collaboration API

Status: spike and design proposal, 2026-09-12. No API, configuration keys,
database migrations, hosting service, or mobile application are implemented
by this document. Endpoint names and component choices are recommendations
to validate before an implementation campaign.

Tracking: [epic #1030](https://github.com/brettbergin/sbxloop/issues/1030)
contains the dependency-linked delivery backlog;
[documentation issue #1031](https://github.com/brettbergin/sbxloop/issues/1031)
covers this spike only. Later implementation issues remain open after the
document lands.

## Purpose and product boundary

Let an authenticated person or application request work, follow its progress,
steer it, resolve a decision, and retrieve its result remotely. `code`,
`workload`, and fixed-recipe `tool` runs must retain their policy, verification, recovery,
and delivery behavior. A disconnected client must not stop autonomous work.

The longer-term product is a shared workspace with project channels. Several
people can shape one effort while individual efforts remain visible to the
appropriate team. Finished work carries a trail back to the conversation,
decisions, execution, evidence, and scoped endorsements that produced it.
Workspace onboarding can use customer-provided inference credentials (BYOK),
with inference billed to the customer's provider account. Hosting execution,
storage, and support still have costs independent of inference.

The first deliverable is a useful remote API for one installation. Workspace
administration, channels, BYOK onboarding, and a hosted gateway extend that
contract in later stages. Collaboration between different customer
workspaces is an open product decision; this spike assumes one owning
workspace per resource and explicitly invited project participants.

Do not add arbitrary remote shell execution, direct sandbox access, raw SQL,
arbitrary host-file access, or a generic proxy to credential-bearing services.
Remote control does not relax target repository protections or workload
profiles. API approval cannot replace a review required by the code host.

## Current foundations and gaps

These observations were refreshed against main at `e4e22f24` before landing
the spike. Read
[the architecture](../architecture.md), particularly Workloads, Persistence
and resume, Events, and the security design principles before implementation.

| Existing seam                                                                                                                                           | What it provides                                                              | What the API still needs                                                        |
| ------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| [`daemon/control.py`](../../packages/sbxloop/src/sbxloop/daemon/control.py)                                                                             | Shared operator dispatcher, local file queue, pending/stale distinctions      | Typed domain results, durable remote operations, authenticated principals       |
| [`daemon/loop.py`](../../packages/sbxloop/src/sbxloop/daemon/loop.py)                                                                                   | Scheduling, holds, cancellation, retry, review waits, merge/publication gates | Resource-specific atomic commands and a coordinated remote resume path          |
| [`daemon/mailbox.py`](../../packages/sbxloop/src/sbxloop/daemon/mailbox.py) and [`daemon/local.py`](../../packages/sbxloop/src/sbxloop/daemon/local.py) | Local console messages, choices, approvals, and read projections              | Public schemas, bounded queries, permissions, durable question lifecycle        |
| [`daemon/model.py`](../../packages/sbxloop/src/sbxloop/daemon/model.py)                                                                                 | Work item distinct from its run                                               | Stable public IDs and provenance independent of source-specific IDs             |
| [`daemon/concierge.py`](../../packages/sbxloop/src/sbxloop/daemon/concierge.py)                                                                         | Workload intake and existing issue creation/labeling paths                    | Explicit API intake using the same admission rules                              |
| [`events.py`](../../packages/sbxloop/src/sbxloop/events.py) and [`engine/store.py`](../../packages/sbxloop/src/sbxloop/engine/store.py)                 | Synchronous event fan-out and persisted run chronology                        | Authorized replay, public envelopes, stream backpressure, durable command audit |
| [`daemon/usage.py`](../../packages/sbxloop/src/sbxloop/daemon/usage.py)                                                                                 | Reported tokens and turns by agent/backend/model                              | Workspace aggregation; provider billing remains a separate source               |

Specific constraints that affect the contract:

- `CommandReply.text` is presentation prose. HTTP clients must not parse it
  to decide whether a command succeeded or what changed.
- The dispatcher accepts `by` for attribution; that is not an authorization
  principal. Chat currently relies on access to the configured channel.
- Daemon `pause` prevents new claims and lets active work finish. Existing
  named holds are in memory, so durable API holds require an intentional
  lifecycle change shared with the existing surfaces.
- Bare daemon `cancel` targets the current run at execution time; named
  cancellation also exists for provider-held work. Remote cancellation must
  cover both states and atomically match the run the person intended.
- CLI `resume` continues persisted execution; daemon `resume <target>`
  resumes a review wait. They are not interchangeable operations.
- Retry and requeue have different attempt-accounting semantics. Preserve
  that distinction rather than hiding both behind a generic restart.
- A database-only status snapshot cannot establish live holds, a claim in
  flight, or all current admission limits. Unavailable live state is unknown.
- The event bus isolates subscriber exceptions. Publishing to it alone is
  not proof that a remote command or audit record was durably committed.
- Streaming message deltas are not persisted; complete messages are. Public
  replay must state which events are durable.
- Stores now use SQLAlchemy and Alembic under `sbxloop/db/`; extend those
  schemas and migrations rather than adding a parallel persistence framework.
- The VCS role facade now lives under `vcs/`; backend capabilities, including
  unknown and unsupported states, must constrain public actions.
- Fixed `tool` recipes have no planning, judging, steering, or chat. Surface
  their tasks, results, cancellation, and eligible resume without introducing
  model turns or arbitrary caller-supplied commands.
- Schedule add/remove, guarded configuration edits, and supervised restart
  already have host services. Later HTTP administration should reuse them.

## Proposed stack and deployment shapes

```text
Web/mobile client, CLI, service account
    |
    | HTTPS: typed JSON commands, queries, authenticated SSE
    v
TLS edge / optional SaaS gateway
    | identity, workspace resolution, request limits
    v
Host API adapter
    | schema validation, object authorization, idempotency
    v
Shared application command/query service
    | durable operations + audit/outbox; no HTTP presentation rules
    v
Daemon: sole scheduler and execution owner
    | host-initiated sbx exec / sbx cp
    +--> agent sandbox: inference credential only
    +--> GitHub sandbox: fixed GitHub operations
    +--> service sandbox, when granted: fixed credentialed operations

Persisted state / public event projection / authorized artifact catalog
    ---> queries, replay, SSE, existing human surfaces
```

Recommended starting components:

| Layer             | Proposed choice                                                                         | Responsibility                                                             |
| ----------------- | --------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| HTTP contract     | Versioned JSON REST and generated OpenAPI                                               | Explicit input/output/error models; SDK generation later                   |
| Host adapter      | FastAPI with Pydantic models, served through Uvicorn                                    | Thin routing and lifecycle integration; optional host API dependency group |
| Commands          | Typed application service shared by HTTP and existing controls                          | Authorization context, transitions, operation completion, audit            |
| Local persistence | Existing SQLite home plus versioned API tables                                          | Operations, public IDs, deduplication, audit, replay projection            |
| Live updates      | Server-sent events (SSE) backed by persisted records                                    | Reconnectable chronology without a second command transport                |
| Artifacts         | Existing run storage behind a catalog                                                   | Authorized metadata and downloads without exposing host paths              |
| Hosted extension  | Gateway, identity integration, relational control store, secret store, artifact storage | Workspace control and routing; does not independently schedule local runs  |

FastAPI is a candidate because its documented features include Pydantic
integration and OpenAPI generation. No framework compatibility or deployment
performance has been tested here. See [FastAPI features](https://fastapi.tiangolo.com/features/).

For the first deployment, run one API server alongside the existing daemon
in the same host process, with isolated I/O and bounded command handling.
There is one daemon owner, never one engine or scheduler per HTTP request
or server worker. Slow GitHub operations execute off the HTTP event loop.
Database connections follow their owning thread's rules. The listener must
not accept mutations until recovery has established execution ownership.

Default exposure is loopback, with authenticated remote access through a
configured TLS boundary. Binding to a broader interface is explicit. Local
binding alone does not establish operator identity or authorization. Only
configured proxies may supply forwarded identity or connection metadata.

A hosted gateway can serve workspace state and cached run history while an
installation is disconnected, but must label the snapshot's age and refuse
actions requiring current daemon state. It must not interpret silence as
idleness, successful cancellation, or permission to dispatch elsewhere.

For customer-hosted execution, an optional connector may originate an
authenticated connection from the trusted host to the gateway. It carries
typed, scoped commands with deadlines and execution acknowledgements. This
is not a tunnel into a sandbox. Routing, fencing of an old host after
reconnection, and duplicate command recovery require a separate validated
transport design before that deployment mode ships.

### Preserve the sandbox security boundary

An HTTP listener on the trusted host does not by itself change the worker
protocol. The proposal is acceptable only if sandboxes still cannot reach
the host API, a gateway acting on its behalf, or another sandbox. Public
reachability through a gateway must not create an alternate route around
the host boundary; verify it against sandbox network policy on CI runners.

The API never installs a listener in a worker VM or gives an agent an
operator token. GitHub and service secrets remain in their designated
credential domains. Requests for host tools still use the existing
host-mediated typed protocol. See [worker protocol](../worker-protocol.md).

## Resource model and versioning

| Resource             | Meaning                                                                                             |
| -------------------- | --------------------------------------------------------------------------------------------------- |
| Workspace            | Owning tenant: membership, policies, provider bindings, and budget authority                        |
| Project              | Collaboration scope within a workspace; may reference configured repositories and workload profiles |
| Work item            | Stable request and intake provenance, potentially attempted by multiple runs                        |
| Run                  | A specific execution with kind, stage, tasks, pinned configuration, and result                      |
| Operation            | Durable record of a remote command and the specific effect it promises                              |
| Gate                 | An outstanding merge/publication decision bound to a target revision                                |
| Steering instruction | Explicitly submitted direction, distinct from ordinary conversation                                 |
| Artifact             | Cataloged output with identity, digest, availability, and authorized content access                 |
| Event                | Attributed observation or transition with a stable replay identity                                  |
| Attestation          | Explicit endorsement of a named claim and immutable subject revision                                |

Use `/v1` on a single-workspace installation. Hosted workspace routes use
`/v1/workspaces/{workspace_id}` followed by the same relative paths. The
gateway resolves the execution owner; the client does not choose a host
by supplying a filesystem path or arbitrary URL. Workspace selection is
validated against the authenticated identity, never trusted as an assertion.

Assign opaque public IDs. A mapping to existing source IDs must include
workspace, repository identity where applicable, source kind, and source
identifier. Two repositories' issue numbers must never alias. Keep public
identity stable across retry, resume, and reconnection. Do not expose raw
database rows, host usernames, local paths, or unrestricted configuration.

Common fields include `id`, `workspace_id`, optional `project_id`,
`created_at`, `updated_at`, and a resource `revision`. Timestamps use UTC
RFC 3339 strings. Enumerate command-relevant states in OpenAPI; clients must
render unfamiliar additive event types without crashing. A run's lifecycle
status, execution stage, gate state, and work item's queue state are distinct.

All collection endpoints support bounded cursor pagination and documented
filters. Return `data`, `next_cursor`, and `has_more`; do not promise exact
counts when expensive. Cursors bind to the authorized scope, sort order,
and filters. A read response's `available_actions` helps the UI, but the
server rechecks permission and eligibility when the action arrives.

Publish `/v1/openapi.json` and `/v1/capabilities` with contract version,
enabled features, server version, request limits, replay retention, and
supported run kinds. Breaking public semantics require a version change.
The public API version is independent of the host-worker protocol version.

## Endpoint catalog

Paths below are relative to the selected `/v1` base. Every resource and
collection is authorization-filtered. Readiness and liveness disclose only
minimal service information; detailed status requires authentication.

### Observe execution and results

| Method | Path                               | Contract                                                                                           |
| ------ | ---------------------------------- | -------------------------------------------------------------------------------------------------- |
| GET    | `/health/live`                     | API process is responsive; says nothing about execution readiness                                  |
| GET    | `/health/ready`                    | Required recovery, state storage, and execution owner are ready                                    |
| GET    | `/status`                          | Live current/claiming state, queue counts, holds, breaker, stopping, version, and observation time |
| GET    | `/items`                           | Work items filtered by state, repository, project, or kind                                         |
| GET    | `/items/{item_id}`                 | Request, origin, current state, and associated runs                                                |
| GET    | `/queue`                           | Shared scheduler ordering and known eligibility reasons                                            |
| GET    | `/runs`                            | Run history with bounded filters                                                                   |
| GET    | `/runs/{run_id}`                   | Run summary, stage, result, gates, and available actions                                           |
| GET    | `/runs/{run_id}/tasks`             | Task graph, attempts, progress, and verification summaries                                         |
| GET    | `/runs/{run_id}/events`            | Durable chronology after a cursor                                                                  |
| GET    | `/runs/{run_id}/artifacts`         | Catalog entries for delivered files                                                                |
| GET    | `/artifacts/{artifact_id}`         | Digest, media type, size, origin, revision, and availability                                       |
| GET    | `/artifacts/{artifact_id}/content` | Authorized file download                                                                           |
| GET    | `/runs/{run_id}/usage`             | Reported tokens and turns by persona/backend/model                                                 |
| GET    | `/usage`                           | Authorized aggregate usage within a bounded time range                                             |
| GET    | `/logs`                            | Restricted, redacted, bounded daemon diagnostics                                                   |
| GET    | `/events`                          | Workspace-scoped durable public chronology                                                         |
| GET    | `/events/stream`                   | SSE from the same durable cursor space                                                             |

`/queue` must use the same ordering and eligibility rules as dispatch, not
the current control command's presentation helper if it differs. Status
includes `observed_at` and freshness. A gateway may return a clearly marked
last-known snapshot, but unavailable live fields remain unknown.

Usage is observed telemetry, not a provider invoice. Preserve missing
values as missing. A later currency estimate needs a labeled pricing basis
and timestamp; do not infer a customer's actual BYOK bill from token totals.

### Admit and control work

| Method | Path                                | Contract                                                                    |
| ------ | ----------------------------------- | --------------------------------------------------------------------------- |
| POST   | `/items`                            | Admit an explicit request through existing source and policy rules          |
| POST   | `/items/{item_id}/retry`            | Fresh attempt for an eligible settled item; existing retry accounting       |
| POST   | `/items/{item_id}/requeue`          | Unpin execution and start fresh, retaining existing attempt accounting      |
| POST   | `/items/{item_id}/abandon`          | Abandon work with an attributed reason and normal source reporting          |
| POST   | `/runs/{run_id}/cancel`             | Cancel this run at a supported boundary; never another current run          |
| POST   | `/runs/{run_id}/resume`             | Continue persisted execution through the sole execution owner               |
| POST   | `/runs/{run_id}/steering`           | Submit explicit direction with source-message revision references           |
| GET    | `/runs/{run_id}/steering`           | Instruction receipt, delivery/handling status, and linked outcomes          |
| POST   | `/runs/{run_id}/round-grants`       | Grant a positive bounded number of additional revision rounds               |
| GET    | `/gates`                            | Outstanding or resolved merge/publication gates                             |
| GET    | `/gates/{gate_id}`                  | Gate revision, subject, evidence, required authority, and available actions |
| POST   | `/gates/{gate_id}/approve`          | Endorse and release the identified gate under policy                        |
| POST   | `/runs/{run_id}/review-wait/resume` | Re-arm the wait for external review and request a check                     |
| GET    | `/operations`                       | Recent authorized operations, filterable by resource and state              |
| GET    | `/operations/{operation_id}`        | Accepted command, promised effect, progress, and terminal outcome           |

There is no generic `PATCH status` or public `POST command` accepting CLI
text. Extract typed application services beneath the existing dispatcher;
keep prose rendering at the CLI/chat boundary. Internal helpers are not
automatically safe public capabilities.

Initial intake has two explicit forms: an existing issue in a configured
repository, or an inline workload ask with an allowed profile. Issue intake
uses normal labeling/admission, claim ownership, conflict checks, and source
reporting. Repeated API requests and issue polling converge on one item.
An inline workload needs a new API source identity integrated with the local
source/reporting paths, rather than pretending to be a chat message.
Direct inline code requests are a later source extension.

A third intake capability may admit a registered `tool` recipe by its public
identity and validated parameters. Reuse the trusted recipe registry and
existing admission rules; never accept an arbitrary command or transform a
recipe into agent work. Observation covers all three run kinds from the
start. Advertise steering, grants, and gates only for kinds and states that
support them; unsupported tool controls fail explicitly.

Code-host actions still execute through the `VcsOps` role facade and its
selected backend (`vcs.op` worker jobs). Target branch
protection, human reviews, baseline comparison, and one-round bot policy
continue to apply. A workload result PR does not become a code run to merge.
Publication continues to use the existing permitted sinks and resume rules.

### Operate the daemon

| Method | Path                              | Contract                                                                                 |
| ------ | --------------------------------- | ---------------------------------------------------------------------------------------- |
| GET    | `/daemon/holds`                   | Attributed holds and reasons preventing new claims                                       |
| POST   | `/daemon/holds`                   | Create a durable hold with owner, reason, and explicit release policy                    |
| DELETE | `/daemon/holds/{hold_id}`         | Release a specific hold subject to ownership or administrative authority                 |
| POST   | `/daemon/stop`                    | Request graceful shutdown after active execution and landing                             |
| GET    | `/repositories`                   | Configured repository metadata and polling health                                        |
| POST   | `/repositories/{repo_id}/resume`  | Resume polling a suspended repository                                                    |
| GET    | `/profiles`                       | Authorized workload profiles and capability limits                                       |
| GET    | `/recipes`                        | Authorized registered tool recipes and validated parameter schemas                       |
| GET    | `/schedules`                      | Configured schedules, paused state, last due, and next due                               |
| POST   | `/schedules/{schedule_id}/pause`  | Pause future firings under existing schedule semantics                                   |
| POST   | `/schedules/{schedule_id}/resume` | Resume future firings under existing schedule semantics                                  |
| GET    | `/configuration`                  | Explicitly allowlisted effective settings and provenance; no secret values or host paths |

Named daemon holds prevent new claims; they do not freeze an active agent
or interrupt a landing already in progress. Releasing one person's hold
does not release someone else's. Persisted holds are a proposed extension
to today's in-memory set; their migration and local/chat parity must ship
before exposing the durable contract. Disconnecting a client never releases
its hold implicitly.

Stopping the API-owning daemon may make its operation endpoint unavailable.
Define stop success as durable acceptance of graceful shutdown, not proof
the process has exited. Service-manager restart policy is reported where
known. Starting/restarting an absent daemon requires an external supervisor
or gateway capability, outside this first API. A request to stop does not
promise it remains stopped under a restarting service manager.

The existing supervised restart service can support a later
`POST /daemon/restart` action while the daemon is reachable. Reuse its
supervisor checks and restart marker; distinguish durable acceptance from
observing a new ready generation. Refuse when no supported supervisor is
configured. This action cannot start a process that is already absent.

Remote configuration writes, repository registration, schedule creation,
backup/restore, garbage collection, and sandbox deletion remain privileged
future administration capabilities. Each needs validation, attribution,
conflict/recovery semantics, and a review of effects on active runs. Do not
publish filesystem-oriented CLI operations mechanically as HTTP endpoints.
Schedule creation/removal and guarded configuration editing already have
host implementations; their future API adapters should share those services.

## HTTP and asynchronous command contract

GET requests never start an agent or mutate execution. Immediate resource
creation returns `201 Created`; completed synchronous updates return `200`
or `204`. Commands requiring asynchronous handling return `202 Accepted`
with an operation representation and `Location` pointing to its monitor.
Acceptance means durable admission, not completion of the requested effect.
These choices follow [HTTP semantics](https://www.rfc-editor.org/rfc/rfc9110.html).

Every mutating POST requires an `Idempotency-Key`. Its scope includes
workspace, principal, method, and canonical target. Persist the validated
payload fingerprint, operation, and deduplication record atomically. Same
key and payload returns the same operation; a different payload returns
`409 idempotency_conflict`. Replays still require current authorization.
Document a bounded deduplication window; after it expires clients reconcile
by resource/operation identity instead of blindly resubmitting.

Sensitive commands carry `expected_revision` for their target resource.
Validate it and the transition atomically with admission. Return
`409 stale_revision` if the person acted on a superseded state. This is an
application precondition for action routes; do not apply a parent object's
ETag to a different action URL as though they were the same representation.
Recheck execution eligibility and authority before starting deferred effects.

Persist server receipt time, optional bounded `expires_at`, and daemon
generation for commands needing a live owner. Expire unclaimed cancellation,
steering, or approval commands rather than applying old intent after a long
outage. Already claimed commands require reconciliation, not an expiration
that falsely implies they never executed. Initial admission while the daemon
is unavailable returns `503`; offline queuing is a future explicit mode.

Example cancellation (illustrative IDs and revisions):

```http
POST /v1/runs/run_123/cancel
Content-Type: application/json
Idempotency-Key: cancel-run-123-01

{
  "expected_revision": 17,
  "reason": "The team changed the scope",
  "retry": false
}
```

```http
HTTP/1.1 202 Accepted
Location: /v1/operations/op_456
Content-Type: application/json

{
  "id": "op_456",
  "action": "run.cancel",
  "target": {"type": "run", "id": "run_123"},
  "state": "accepted",
  "effect": "run reaches cancelled state",
  "actor": {"type": "user", "id": "user_7"},
  "accepted_at": "2026-09-12T18:00:00Z"
}
```

Operation states are `accepted`, `running`, `reconciling`, `succeeded`,
`failed`, and `expired`. An ambiguous external effect remains `reconciling`
with a reason and an operator recovery path. Never claim exactly-once effects
across a database and a code host. Do not add general operation cancellation
until each command defines whether withdrawal is possible.

| Action          | Operation success means                                        | Separate downstream outcome                                    |
| --------------- | -------------------------------------------------------------- | -------------------------------------------------------------- |
| Admit item      | One item is durably admitted through its source rules          | A run starts and eventually finishes                           |
| Cancel run      | This run reached cancellation                                  | Source reporting may still be pending                          |
| Submit steering | Instruction is durably handed to the intended run's input path | Handling/reply and any plan change have separate linked events |
| Grant rounds    | Grant and any required re-admission are committed              | Further execution may still fail                               |
| Approve gate    | Scoped approval is recorded and the gate release is committed  | PR merges or workload publishes later                          |
| Stop daemon     | Graceful stop intent is committed and signaled                 | Process exit/restart is supervisor state                       |

If cancellation loses a race to completion, report a structured
`target_already_terminal` outcome with the actual state; do not label a
completed run cancelled. If steering is never handled, preserve the receipt
and report undelivered/superseded status instead of implying it changed work.

Errors use `application/problem+json` with `type`, `title`, `status`,
`detail`, `instance`, and extensions `code` and `request_id`, following
[RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html). Define `400` for
malformed input, `401` for missing/invalid authentication, `403` for known
but forbidden operations, `404` for missing or concealed resources, `409`
for state/idempotency conflicts, `410` for expired replay history, `422` for
invalid command fields, `429` for admission limits, and `503` for unavailable
execution authority. Responses and logs never echo credentials.

## Persistence, recovery, and execution ownership

Add versioned storage for public resource mappings, API operations,
idempotency records, durable holds, and an audit/event outbox. Keep state
paths under `SbxloopHome`. The first implementation can retain SQLite;
a hosted control store is a separate service with its own migrations.
Do not share one daemon database across unrelated tenants.

Define the durability failure model explicitly. The current engine store's
WAL configuration must not be assumed to guarantee that every acknowledged
write survives power loss. Acceptance, deduplication, and audit commits need
an fsync-backed durability policy if that guarantee is offered. Validate
the chosen connection settings and their effect on engine write latency.

The required command sequence is:

1. Authenticate, resolve workspace, authorize the target and referenced
   objects, validate the command and preconditions.
2. Transactionally admit the operation with its deduplication record and
   acceptance audit entry. Only then acknowledge acceptance.
3. The daemon claims the command under its execution ownership, rechecks
   eligibility and current authorization, and records progress.
4. Apply the domain transition through shared services. For external
   effects, persist intent before dispatch and reconcile observable state
   after interruption. Reuse existing source claim and gate recovery rules.
5. Commit the result and audit/outbox entries before publishing them.

Existing engine events need a replay projection, but the event bus must not
become the transaction coordinator. API acceptance/decision records require
checked persistence; a swallowed subscriber failure cannot acknowledge
success. Keep new public audit envelopes separate from the worker contract
so the byte-identical code-run trail fixture remains meaningful.

Crash tests must cover acceptance before reply, claim before effect, effect
before result, and result before stream delivery. A reconciler inspects
pending operations on restart. Gate and source-state evidence decide whether
work happened; a timeout is never proof that it did not.

Resume is admitted to the daemon, never launched as a second engine from an
HTTP handler. It atomically checks the item's pinned run, execution lease,
persisted configuration, budgets, and stage. Restored operations preserve
the distinction between resuming execution and re-arming external review.
Multiple API readers are possible; multiple execution owners require a
separate lease/fencing design and are not implied by horizontal HTTP scaling.

## Live events and client synchronization

`GET /events` and `/events/stream` share an authorized durable cursor space.
Use SSE `id` fields and reconnect with `Last-Event-ID`, as described by the
[HTML SSE standard](https://html.spec.whatwg.org/dev/server-sent-events.html).
The server supplies replay; the transport alone does not guarantee it.

A public event envelope contains `id`, `schema_version`, `type`, `occurred_at`,
`recorded_at`, workspace/project scope, resource references, optional actor,
`operation_id`, `causation_id`, and typed `data`. Existing run events may have
unknown actor/causation; do not fabricate historical attribution. Preserve
their native sequence reference for traceability.

Snapshots provide a high-watermark cursor; replay starts after that point
without a snapshot/subscription gap. Durable state and projection ordering
must support that promise. When a gateway projection is eventually
consistent, disclose its watermark and let clients follow an operation to
the authoritative result. Clients deduplicate repeated event IDs. No global
causal order is claimed between independent installations.

Disconnect slow consumers with a resumable cursor instead of blocking an
engine thread. Bound per-client buffers, connections, replay pages, and
message sizes. Heartbeats carry no domain meaning. If history was pruned,
return `410 cursor_expired` with a fresh snapshot path; never silently skip
missing history. Temporary token deltas are optional and explicitly
non-replayable; complete messages remain recoverable.

Authenticate streams and apply the same object permissions as ordinary
reads. Re-evaluate access on membership/token revocation and close affected
streams. Use secure browser sessions or an authenticated fetch-based SSE
client for token clients; no long-lived tokens in query strings. Scope
cross-origin access explicitly and protect cookie-authenticated mutations
against cross-site requests. WebSockets and push notifications are future
transports. A push notification is a prompt to refresh current state, never
authority to apply a stale approval.

## Identity, authorization, and tenancy

Start with revocable, scoped operator/service tokens for a single
installation; hosted web/mobile identity can later use an identity provider
and short-lived sessions. Model-provider keys are never sbxloop login
credentials. Derive actor identity from authentication and bind it to the
operation, including the service account and any verified delegation.
Client-supplied display names or message author fields cannot grant authority.

Bootstrap the first installation administrator through an authorized local
setup path; ship no default credential. Store API token verifiers rather
than reusable token plaintext, show newly issued tokens once, and support
explicit expiry and revocation. Token issuers cannot grant capabilities they
do not hold. Authentication failures are rate-limited independently of work
admission. A hosted identity integration needs issuer/audience validation,
secure browser sessions, and an appropriate native-client authorization flow;
select and verify that integration before mobile onboarding ships.

Draft capabilities, independent of role names:

| Capability           | Allows                                                           |
| -------------------- | ---------------------------------------------------------------- |
| `runs:read`          | Authorized runs, their events, and non-sensitive summaries       |
| `artifacts:read`     | Authorized artifact content, independently controllable          |
| `items:create`       | Start work under granted repositories/profiles/budgets           |
| `runs:steer`         | Explicit steering of authorized active efforts                   |
| `runs:control`       | Cancellation, eligible resume, retry, and requeue                |
| `gates:approve`      | Gate-kind and subject-specific endorsements allowed by policy    |
| `budgets:grant`      | Additional execution allowance within administrative limits      |
| `daemon:manage`      | Holds, schedules, repository polling, and graceful stop          |
| `credentials:manage` | Bind, rotate, and revoke provider credentials; never read values |
| `audit:read`         | Authorized decision and operator history                         |
| `diagnostics:read`   | Restricted logs and configuration summaries                      |

Role presets can group these capabilities, but project/repository/profile
constraints apply to every action. A reader is not automatically a spender;
a contributor is not automatically an approver. Agent principals cannot
self-assign human approval capability. Administrative identity does not
bypass target code-host requirements.

Apply authorization at object lookup, list filtering, event replay, artifact
download, nested source references, and deferred execution. Resolve all
resource relationships within workspace scope. Policy-controlled spending
and credential use need an explicit sponsor. Tenant ID in a URL alone is
not isolation. Enforce tenant separation in storage, queues, caches, secret
bindings, artifact namespaces, and runtime routing as well.

For initial hosted execution, allocate separate daemon homes and execution
owners per workspace. Membership revocation blocks new commands and closes
access promptly; already executing effects require explicit cancellation
or credential revocation, whose limits must be reported. Define whether a
departed owner's accepted but unclaimed commands expire or need reassignment;
the proposed default is refusal pending authorized re-admission.

Cross-workspace projects would require explicit ownership of shared content,
execution sponsorship, export/retention rules, and grant revocation. Do not
inherit access transitively through a shared channel or silently share keys.

## BYOK and artifact handling

The proposed onboarding model binds a named provider credential to a
workspace. Only authorized users can select it for execution under project
policy. Model requests use the customer's provider account; provider-specific
API access, backend compatibility, billing attribution, and key restrictions
remain field-unverified until tested for each supported integration.

Store values through a secret-store abstraction, encrypted at rest in a
hosted deployment, and return metadata only. Secret submission bodies are
excluded from request logging, tracing, error echoes, and analytics. Events
carry credential IDs/names and versions, never values. Existing host-to-worker
injection continues through the designated credential path, never `sbx` argv.
Raw inference credentials remain subject to the current agent-domain model;
other operator credentials must not enter that domain.

Rotation records the binding version used by each run without exposing the
value. Revocation blocks new use; already injected credentials may require
provider revocation and execution teardown. Report that distinction. Apply
workspace/project admission limits and record sponsored usage. BYOK removes
inference resale from this proposal, not compute/storage costs or the need
to prevent accidental spending. Hard currency ceilings require reliable
provider accounting; initially enforce measurable run/turn/token limits
and clearly report their granularity and possible in-flight overshoot.

Artifacts are immutable cataloged revisions with digest, size, media type,
run/task origin, and availability status. Resolve content inside an approved
artifact root and reject traversal, symlink escapes, and arbitrary host paths.
Default to attachment download with safe content headers; active HTML output
must not execute on the authenticated control origin. A hosted object-store
download grant must be narrow and short-lived. Explain if an issued download
grant cannot be revoked before expiry. Expired/deleted content leaves an
authorized tombstone so the history does not imply it is still retrievable.

## Collaboration and the decision trail

The later collaboration layer records conversation separately from execution
intent. A message may propose, question, or discuss work without authorizing
an agent. Explicit submission links an instruction or work item to the exact
message revisions used as context. A model-generated summary links its
sources and labels inferences; it is not a substitute for a person's consent.

Candidate hosted routes, relative to the workspace base unless noted:

| Method        | Path                                           | Purpose                                                                        |
| ------------- | ---------------------------------------------- | ------------------------------------------------------------------------------ |
| GET           | `/v1/me` (global)                              | Current authenticated identity                                                 |
| GET, POST     | `/v1/workspaces` (global)                      | List authorized workspaces or create one under onboarding policy               |
| GET, POST     | `/memberships`                                 | List members or invite a participant with bounded grants                       |
| PATCH, DELETE | `/memberships/{membership_id}`                 | Change grants or revoke membership with audit                                  |
| GET, POST     | `/service-tokens`                              | List token metadata or issue a scoped token once under administrative policy   |
| DELETE        | `/service-tokens/{token_id}`                   | Revoke API access without erasing attribution history                          |
| GET, POST     | `/projects`                                    | Project metadata and collaboration scope                                       |
| GET, POST     | `/projects/{project_id}/channels`              | Project channels                                                               |
| GET, POST     | `/channels/{channel_id}/messages`              | Authorized conversation and attributed message creation                        |
| GET, POST     | `/messages/{message_id}/revisions`             | Read history or append an explicit correction                                  |
| GET           | `/questions`                                   | Outstanding questions with target, revision, and expiry                        |
| POST          | `/questions/{question_id}/answers`             | Explicit answer to the current question revision                               |
| GET, POST     | `/decisions`                                   | Record claims, alternatives, accepted direction, and source revisions          |
| GET, POST     | `/attestations`                                | Scoped endorsements bound to immutable subject revisions                       |
| POST          | `/attestations/{attestation_id}/retract`       | Append attributed retraction; preserve the original record                     |
| GET           | `/items/{item_id}/provenance`                  | Authorized graph of discussion, decisions, operations, execution, and evidence |
| GET, POST     | `/provider-credentials`                        | Read binding metadata or submit a new secret binding                           |
| POST          | `/provider-credentials/{credential_id}/rotate` | Replace the binding value/version without returning it                         |
| DELETE        | `/provider-credentials/{credential_id}`        | Revoke future use and report any active-use limitation                         |
| GET, POST     | `/exports`                                     | Inspect or request an authorized history/artifact export                       |

Generic attestations record endorsements; they do not themselves merge,
publish, or change execution authority. `/gates/{gate_id}/approve` checks
policy and may create the corresponding attestation as part of its atomic
release. A design endorsement does not imply security review or acceptance
of every requirement. Later changes create new subjects/revisions and may
require new approval; the original endorsement remains attached to the old
subject. Ordinary comments and emoji reactions never silently become approvals.

Each record distinguishes who spoke, who authorized an action, which service
executed it, what evidence was checked, and what remains inferred or unknown.
Keep rejected alternatives and superseded decisions where permitted, so
the history explains changes of mind rather than constructing false consensus.
Linking a private message does not broaden permission to read it.

Append-only application semantics support accountability but do not prove
tamper resistance against a database administrator. Cryptographic signing,
external witnessing, and compliance-grade attestations are separate design
questions. Retention, redaction, deletion, and export must be explicit:
preserve authorized tombstones and provenance gaps instead of promising to
retain every original conversation forever. Avoid deriving individual
performance scores from attribution as an implicit feature.

## Operational limits, configuration, and observability

Limit request bodies, list windows, stream connections, pending operations,
concurrent downloads, and execution admission by workspace and principal.
Return actionable limit errors with retry information where appropriate.
Use bounded queues so remote polling, reconnect storms, or large outputs
cannot starve the daemon. An API outage does not cancel ongoing work;
an unrecordable authorization/decision fails closed before new effects.

Track request latency/errors, pending operation age, reconciliation backlog,
daemon generation/liveness, outbox lag, replay gaps, disconnected consumers,
admission refusals, and artifact errors. Correlate request, operation, item,
run, and event IDs. Log stable structured fields and redacted diagnostics;
exclude message bodies and credentials by default. Operators need to tell
an unavailable daemon from a paused one and a failed command from an unknown
external outcome.

Proposed configuration categories are listener binding/TLS trust, identity,
request limits, replay/idempotency retention, operation deadlines, and
artifact delivery. This spike chooses no config keys or numeric defaults.
Each eventual knob must land in the config model, packaged example, the
`docs/user-guide.md` knob table, and tests, with per-repository overrides
where applicable and README entry points kept current. Existing
headless, CLI, and chat operation remains available when the API is disabled.

API schema, database schema, host/worker compatibility, and gateway protocol
versions need independent checks. Startup migration failure keeps readiness
false. Publish upgrade/rollback limits before exposing durable state to
multiple versions of clients. Do not claim rollback can read a newer schema
without a demonstrated compatibility path.

## Delivery sequence and validation

This is an investigation sequence, not authorization to implement a stack.
Each stage ships its own documentation, recovery behavior, and relevant
tests. Existing controls remain the comparison baseline.

1. **Typed control foundation.** Extract structured command/query results,
   principal context, run-specific cancellation, and a coordinated resume
   path. Establish durable operations, idempotency, audit, and hold behavior.
   Verify CLI/chat parity before adding HTTP.
2. **Complete remote run loop.** Ship authentication, status, intake, run/task
   reads, replay/SSE, steering, cancellation, gates, artifacts, and operation
   monitoring. Include a source-aware admission path and no hidden code-only
   assumptions. Publish the first OpenAPI contract.
3. **Administrative coverage.** Add remaining queue/item controls, schedule
   controls, repository polling recovery, usage, diagnostics, and graceful
   stop. Validate restart semantics and bound every query/stream.
4. **Hosted workspaces and BYOK.** Add identity integration, membership and
   project scope, tenant-isolated execution routing, credential management,
   budgets, and exports. Validate hosted and customer-hosted modes separately.
5. **Collaborative decision records.** Add channels, durable questions,
   decisions, attestations, and provenance views. Verify multiple people can
   contribute without implicit authorization or loss of dissent/context.

Required implementation test scenarios include:

- Two identities with different project/repository grants; attempts to read
  another tenant through lists, IDs, cursors, nested references, or downloads.
- Duplicate admission through HTTP and source polling, concurrent identical
  requests, changed payload under one idempotency key, and lost HTTP replies.
- A delayed cancellation of run A after run B starts; terminal-state races;
  resume while another owner is executing; membership removal before claim.
- Independent holds, releases by another user, restart with holds, graceful
  stop, and readiness while claim recovery is in progress.
- Stale/repeated gate approvals, protected-base reviews, changed publication
  artifacts, and a gate released before external completion is observed.
- Crashes at each command/effect boundary, outbox persistence failure, and
  external outcomes that cannot yet be established.
- SSE replay overlap, snapshot gaps, revoked stream access, slow consumers,
  expired cursors, and complete-message recovery without token deltas.
- Artifact traversal/symlink attempts, expired content, unsafe inline output,
  credential redaction, and rotation/revocation during active work.
- All three run kinds, workload profile refusal and held publication,
  trusted recipe parameter validation and unsupported tool controls, and the
  byte-identical `tests/unit/test_code_run_trail.py` and
  `tests/unit/test_tool_run_trail.py` fixtures.

Run repository gates in `AGENTS.md` for implementation changes. Behavior
changes need a test that failed first; use the full fast suite for commits
and full tests or complete CI before merge. Extend
`tests/fakes/fake_github.py` for new code-host behavior instead of stubbing
the ops layer. Sandbox network and isolation behavior is verified only on
CI runners, never on a maintainer's or customer's machine.

## Decisions to resolve before implementation

| Decision                   | Proposed starting position                                        | Evidence needed                                                 |
| -------------------------- | ----------------------------------------------------------------- | --------------------------------------------------------------- |
| API deployment             | Optional server alongside one daemon owner                        | Thread/lifecycle prototype and restart recovery tests           |
| Durable command store      | Local versioned tables with checked acceptance/audit transactions | Crash tests and boundaries of existing engine transactions      |
| Tenant execution ownership | One workspace per daemon home/owner                               | Isolation, resource-cost, and routing validation                |
| Remote resume              | Daemon-owned admission of a pinned run                            | Precise interaction with current CLI resume and item settlement |
| Pause persistence          | Durable named holds across surfaces                               | Migration and intentional service restart behavior              |
| Approval subject           | Exact gate/claim revision, with explicit scope                    | Behavior when code/base/artifacts change during landing         |
| Human authentication       | Scoped tokens first; hosted identity later                        | Session/revocation requirements for browser and mobile clients  |
| BYOK compatibility         | Explicit per-provider/backend binding                             | Provider-supported access and observed billing attribution      |
| Offline commands           | Refuse by default; expire stale unclaimed intent                  | Product need for queued offline intake and safe sponsorship     |
| Cross-workspace projects   | Defer; one owning tenant with explicit guests                     | Ownership, payment, retention, and revocation agreement         |
| History guarantees         | Attributed linked records with declared gaps                      | Retention/export policy and need for stronger tamper evidence   |
| Framework and hosting      | FastAPI/Pydantic/Uvicorn candidate; no provider selected          | Packaging, supported runtime, load, and security validation     |

External standards cited here were read during the spike. Proposed provider
integrations, mobile behavior, hosted infrastructure, runtime compatibility,
and sandbox network behavior have not been exercised; describe them as
field-unverified in implementation PRs until the appropriate verification
exists. This document establishes the intended contract and its unresolved
boundaries, not a claim that the full stack already works.
