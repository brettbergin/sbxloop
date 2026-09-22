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

### Sign-in through an OpenID Connect provider

With `[api.oidc] enabled = true` (feature `auth.oidc`), a browser client signs
people in through the provider. `GET /v1/auth/providers` needs no token and
answers:

```json
{"local": true,
 "oidc": {"id": "authentik", "label": "Authentik",
          "authorize_url": "https://auth.example.com/application/o/authorize/",
          "client_id": "angie", "scopes": ["openid", "email", "profile"],
          "end_session_url": "https://auth.example.com/application/o/angie/end-session/"}}
```

`oidc` is `null` when the section is off or the provider's discovery document
cannot be read (a failed read is retried at most every 30 seconds). The client
runs Authorization Code + PKCE against `authorize_url` with its own `state`,
`nonce` and `code_challenge`, then posts the code, without a bearer token:

```bash
curl -s -X POST http://127.0.0.1:8420/v1/auth/oidc/token \
  -H 'Content-Type: application/json' \
  -d '{"provider":"authentik","code":"…","code_verifier":"…","redirect_uri":"https://angie.example.com/auth/callback","nonce":"…"}'
```

The daemon redeems the code at the provider's token endpoint as the
confidential client (`client_secret_basic`, or `client_secret_post` when that
is all the provider offers), validates the ID token (an asymmetric algorithm
from `algorithms`, the provider's published key, `iss`, `aud`, `exp`/`iat`/`nbf`
within `leeway_s`, `azp` when present, a `sub`, and a `nonce` equal to the
request's), creates the account on a first sign-in, and answers with the same
`TokenResponse` a local login returns; refresh and revoke work as for any
other client. Refusals: `400 oidc_invalid_request` (unknown `provider`, or a
`redirect_uri` that is not exactly one of `redirect_uris`; the provider is not
called), `401 oidc_exchange_failed` (the provider refused the code, or the ID
token did not check out; the message is generic), `403 oidc_not_allowed`
(outside `allowed_groups`), `403 oidc_account_disabled` (inactive, or removed
from the workspace), `403 oidc_not_provisioned` (unknown person with
`auto_provision = false`), `409 oidc_account_conflict`
(a concurrent first sign-in; retry), `429 too_many_attempts`, and
`503 oidc_unavailable` (discovery, keys or token endpoint unreachable, or the
client secret is not set). See the [user guide](user-guide.md#sign-in-with-an-oidc-provider-authentik)
for the configuration and role mapping.

| Resource    | Routes                                                         | Purpose                                                      |
| ----------- | -------------------------------------------------------------- | ------------------------------------------------------------ |
| Profile     | `GET/PATCH /v1/users/me`                                       | Local identity and timezone                                  |
| Agents      | `/v1/agents[/{slug}]`, `POST /v1/agents/{slug}/archive`        | Built-in, configured and saved agents; saved ones are edited |
| Teams       | `/v1/teams[/{id}]`                                             | Durable named groups of agent roles                          |
| Memories    | `/v1/agents/{slug}/memories[/{id}]`                            | What each agent keeps beyond one conversation                |
| Channels    | `/v1/channels[/{id}]`                                          | Revisioned conversation containers; deletion tombstones them |
| Messages    | `GET /v1/channels/{id}/messages`, `PUT .../{message}/reaction` | Ordered history and persistent message feedback              |
| Turns       | `POST /v1/channels/{id}/turns`, `GET .../{turn}`               | Idempotent input acceptance and durable completion state     |
| Preferences | `/v1/prompts`, `/v1/prompts/definitions`                       | Prompt context saved for the local user                      |
| Workflows   | `/v1/workflows[/{id}]`                                         | Workflow metadata used by the Angie management screen        |
| Connections | `/v1/connections`, `/v1/connections/services`                  | Redacted status and owner management of host integrations    |

### Agents

`GET /v1/agents` lists every agent a client can address, in merge order: the
built-ins (Angie and the planner, builder, critic and operator), then the
operator's `[[agents]]` from `sbxloop.toml`, then the agents people saved.
`GET /v1/agents/{slug}` also answers an alias and a retired built-in name.
Beside the original fields, each entry carries its identity (`avatar`, a
`#rrggbb` `color`, `aliases`), its narrowing (`roles`, `tools`, `skills`,
`mcp`, `credentials`, `interests`, `can_start`, `max_runs_per_day`),
`enabled`, `source` (`builtin`, `config` or `user`), `editable` and
`revision`. Clients that see `agents.registry` among the capability
features may edit saved agents; the fields are defaulted, so older clients
keep reading the same shape. `GET /v1/agents?include_disabled=true`, which
needs `collaboration:write`, also lists disabled and archived agents and the
retired built-in names, so a client can find an agent that was switched off
and send `PATCH` with `enabled: true` to switch it back on (an archived agent
stays archived).

| Method  | Path                        | Body                                   | Result                              |
| ------- | --------------------------- | -------------------------------------- | ----------------------------------- |
| `POST`  | `/v1/agents`                | the agent spec (`slug`, `name`, ...)   | 201, the agent at `revision` 1      |
| `PATCH` | `/v1/agents/{slug}`         | `expected_revision` and changed fields | 200, the agent at the next revision |
| `POST`  | `/v1/agents/{slug}/archive` | none                                   | 200, the agent with `enabled` false |

All three need `collaboration:write`. A saved agent never takes a slug or an
alias a built-in, configured or other saved agent already has, and a body
naming a key the spec does not have (a host list, an egress rule) is
refused: egress stays the operator's `[policy]`. A spec that names an
undeclared tool, `[[credentials]]` entry or `[[mcp]]` server, or has no
name, answers 422 `invalid_agent` with the reasons in `detail` and
`problems`. A `model` is checked against the configured backend's
discovered model catalog when one is cached (`sbxloop list-models` refreshes
it); with no catalog the name is accepted as given and a wrong one
surfaces in the run that uses it. A built-in or configured agent answers
409 `agent_read_only`; a stale `expected_revision` answers 409
`agent_revision_conflict` with `current_revision`; a slug already saved
answers 409 `agent_exists`, and an archived agent 409 `agent_archived`.
Agents and teams share one mention namespace: saving an agent whose slug or
new alias is a team's slug, or creating or renaming a team to a slug or
alias an agent (enabled, disabled or archived) answers to, is refused with
409 `slug_taken`.
An archived agent is left out of the listing, can no longer be named in a
team or mentioned, and still answers `GET /v1/agents/{slug}`.

A saved agent is addressed like a built-in: `@slug` in a turn, a
`target_slugs` entry or a team member. It answers in a chat session under
its first run role (Angie's own session when it has none) with its own
persona. When the agent sets `model`, its turns use that model instead of
the role's configured one, and its `model_source` reads `agent.model`. An
agent's `handoff_agent` tool may address any enabled agent, saved ones
included; a disabled or archived agent is refused.

An ordinary turn talks to Angie without host or MCP action tools. A known
`@agent`, an enabled `@team`, explicit `target_slugs`, or `intent=delegate`
records work intent and enables the corresponding concierge tools. Product
clients can instead send `intent=code` or `intent=workload` to select one of
sbxloop's managed runners explicitly. Angie coordinates that turn without
seeding agent mentions as parallel chat participants: the code runner owns its
decompose/build/review/fix/CI/merge lifecycle, and the workload runner owns its
plan/execute/judge/revise/publish lifecycle. Explicit runner intents cannot be
combined with `target_slugs`.

A mention is a request to reply. It records the agent as a target and joins it
to the channel, but it no longer rewrites the turn's `intent`: a turn sent as a
`conversation` stays one. The mentioned agent keeps its read tools but is not
offered the tools that start managed work, so it answers in the chat, and says
which intent to pick when the ask needs execution, external sources, a
repository change or a produced file. A turn that may start work (`delegate`,
`code`, `workload` or `auto`) is told to answer whatever the reply itself can
satisfy — a list, an explanation, a short plan, an opinion, a judgement about
work already in the channel — and to start managed work only for those asks.
`TurnOut` carries the recorded `intent` back.

When `/v1/capabilities` lists `collaboration.lead_orchestrator`, `intent` also
accepts `auto`: the client does not know whether the ask is a question or a
piece of work, and the lead decides for that turn whether to answer, start a
code run or start a workload. `auto` accepts mentions and `target_slugs` the way
a conversation does.

On a turn that may start managed work (`intent` `code`, `workload` or `auto`),
the agents the message mentions that declare a run role are recorded on the
first entry of `participants` as `assignees`, a `role -> agent slug` map, and
work admitted from that turn is assigned from it. Other turns leave `assignees`
null.

Team members receive separate role-scoped sessions and their replies are
persisted as separate messages. Conversational peers choose their own bounded
handoffs and may return review findings to an author or coordinator for revision
and final synthesis; the transport does not encode a role sequence. Repeating a
`client_turn_id` returns the accepted turn; reusing it for different text,
targets, intent, or message identity is rejected.

Messages include a `reactions` array. User inputs receive `⏳` when accepted,
then replace it with `✅` after successful completion or `⚠` after failure or cancellation.
Clients can persist user feedback on any message with
`PUT /v1/channels/{id}/messages/{message}/reaction` and a body such as
`{"emoji": "👍", "active": true}`. Repeating the same request is idempotent;
set `active` to false to remove the reaction.

When `/v1/capabilities` lists `collaboration.message_authors`, every message
also carries an `author` object: `{"kind", "id", "display_name"}`. `kind` is
`human` (a person; `id` is the user id and `display_name` their full name, or
their username when none is set), `agent` (`id` is the agent slug and
`display_name` its registry name, so `concierge` shows as `Angie`), or
`system` (turn error and stop notices; `id` and `display_name` are null).
Messages written before authorship was recorded report the author they always
had: the channel's user for user input, the named agent for agent replies and
handoffs, Angie for replies and work results that name no agent. The existing
`role` and `agent_slug` fields are unchanged. Turns report the same person as
`author_id`, with `trigger` (`human` for every turn a person submits) and
`parent_turn_id` (null until agents can start turns of their own), and
`collaboration.message.created` events carry `author_kind` and `author_id`.
Each channel records its creator as its owner member; who else may open it
is described under "Channel access, members and participants" below.

The event stream records `collaboration.participant.running`,
`collaboration.tool.started`, and `collaboration.tool.completed` as the work
happens. Tool events include the channel, turn, participant index, agent slug,
tool name, and completion `ok` flag; they omit arguments and result contents.
Clients can use these events to refresh the channel's authoritative active
turns and messages, rather than infer progress from message text. Ordinary
conversation with no advertised host tools submits no host tool handler.

Successful Code issue creation or queueing records its exact repository and
issue identity on the originating participant. `GET /v1/channels/{id}/work`
projects that association even after the conversation turn finishes. Before
the source admits the issue, its state is `awaiting_dispatch`, its item ID is
a provisional `pending_code:` identity, and no controls are offered. After
admission it reports the public item/run IDs, actual stage and available
controls. Completion delivers the recorded PR link or failure to that channel.
The association is durable turn data, independent of event retention; unrelated
repositories with the same issue number do not match. No source polling or
runner behavior changes.

### Conversations for externally started work

`collaboration.external_work` advertises automatic conversations for jobs
known to the connected daemon, including issue labels, schedules, chat
bridges, API admissions and standalone persisted runs. A job without an
existing conversation gets a workspace-visible channel. An existing
association keeps its channel and access rules. Repeated attempts at the
same issue share a conversation; separate schedule occurrences do not.
The association is presentation data: it does not change the item's
admission channel, assignment, scheduling, accounting or source delivery.

Clients with this feature use `GET /v1/channels/{id}/jobs`, a list of
attempt snapshots. Each has a stable `work_id`, optional real `item_id`,
`run_id` and `turn_id`, state, source, revision, available actions and
artifacts. A run with no admitted item has no item controls. The response
also includes work awaiting Code issue admission. The existing `/work`
contract is unchanged and remains the fallback for older daemons.
An attempt whose execution record was removed remains listed with
`unavailable: true`, its recorded metadata, and no controls or artifacts.
Item list/detail responses provide a nullable `channel_id` only when the
viewer can read that conversation.

System-created channel summaries include `external_work` metadata for
sidebar status and source links without loading each transcript. Opening,
progress and result messages identify the system as their author and carry
`source_work_id`, an optional `source_run_id`, and `historical`; they do not
invent a human turn. Channel chat and live-run steering use the ordinary
permission checks. Events and artifacts resolve through the attempt's own
channel, so admitting a later attempt to a private channel does not move
an earlier attempt's history or expose the later attempt there.

The initial import includes unfinished work and terminal work from the
last 30 days. Imported messages are quiet history and do not add unread
counts. New jobs and activity remain unread until read. The scoped event
`collaboration.external_work.attention` carries `channel_id`, `work_id`,
optional `run_id`, a durable `attention_id`, `kind` (`work`, `failure` or
`action_required`), `title`, `body` and `historical: false`; clients apply
their channel preferences and browser-notification opt-in. Reconciliation
and event replay do not resend the same transition. Deleting a generated
channel hides it without recreating it on the next reconciliation.

### Admitting work for named agents

`POST /v1/items` takes three optional fields on an `issue` or `workload`
body (advertised as `intake.assignment`): `lead`, the agent that leads the
run; `roles`, an object mapping a run role (`planner`, `builder`, `critic`,
`operator`) to an agent slug; and `channel_id`, the channel the work answers
to. Each named agent must exist, be active (not disabled or archived) and
declare the role it is asked to take (`lead` for the lead); anything else is
`422 invalid_argument` naming the agent and the role, and nothing is queued
or labelled. Naming a `channel_id` also takes `collaboration:write` (and
`collaboration:read` for a workspace member's client), checked first: without
it the request is `403 forbidden`. A `channel_id` the caller cannot read is
`404 channel_not_found`. Work asked for again after its last run finished is
planned afresh from the new request's lead and roles.
A body without them admits work exactly as before.

When the item is dispatched, the daemon plans its assignment: each role takes
the agent asked for and the built-in agent otherwise, and the lead is the one
asked for or Angie. The plan is stored with the item, and every later attempt
at the same item reuses it, even if an agent was archived since. Issues found
by polling run with the built-in team. Items read back with `lead_agent` (the
planned lead once dispatched, the requested one before) and `assignment` (the
agent in each run role, or `null` when none were named and nothing is planned
yet).

A chat turn passes its channel and, for the agents it mentioned, the run roles
they declare to the work it starts: a workload it queues carries them, and an
issue it files or labels leaves a note the polled item picks up. The note is
spent by the item it fills, so an old conversation's request is never replayed
onto work the same issue is labelled for later. A turn answered by Angie names
Angie as the lead. An item that names its channel is delivered there even when
its key names no message in it (as part of the channel's latest turn when it
was admitted), and never to a channel other than its own; that holds for an
issue (`code`) admission too, whether or not any turn in the channel named the
issue. With `collaboration.external_work`, a channel that has had no turn
yet receives system-authored progress and results through the job projection.
Older daemons skip that delivery and log `api.work_delivery_skipped`.
A turn-associated work result is credited to the item's lead
when it has one, and to the participant that asked otherwise.

A finished workload or tool run's files are catalogued before its work result
is written, so the first `work_result` message already names them. Its `work.artifacts` (and
each entry of `GET /v1/channels/{id}/work`) lists up to 50 available files,
ordered by path, as `{id, run_id, relpath, media_type, size}`: `id` is the
catalog identity served by `GET /v1/artifacts/{id}` and `run_id` is the public
run ID. Files the retention sweep removed are left out. The message text ends
with a `Files:` list of the same paths, for surfaces that show only text. A
result written before this field reports an empty list. The feature is
advertised as `collaboration.message_artifacts`. A code run delivers a pull
request, so its checkout is never listed and its `artifacts` stays empty.

### Files a channel can see

The same files are attached to the message itself: `MessageOut.artifacts` is
the list of `{id, run_id, relpath, media_type, size}` that message carries,
served on `GET /v1/channels/{id}/messages` and empty for every message that
carries none.

`GET /v1/channels/{id}/artifacts` lists every file the channel's messages
carry, newest message first, as `{"data": [...]}`, and
`GET /v1/channels/{id}/artifacts/{artifact_id}/content` serves one file's
bytes as an attachment. Both take `collaboration:read` and channel read
permission rather than `artifacts:read`, so a workspace member who can read
the channel can read the files delivered into it. Both resolve through that
same list, so a file the channel does not carry is `404` there whatever else
the caller may read -- including a catalogued file of a run the channel started
but never delivered here, such as a code run's checkout.
`GET /v1/runs/{id}/artifacts`, `GET /v1/artifacts/{id}` and
`GET /v1/artifacts/{id}/content` are unchanged and still take
`artifacts:read`. The channel routes are advertised as
`collaboration.channel_artifacts`.

An agent answering in a channel gets one more host tool,
`read_channel_artifact(artifact_id, offset=0, limit=64000)`. It is offered to
every participant, read-only roles included, so a critic can read the file it
is reviewing. It resolves an id only when a message in *this* channel carries
it, or when a run this channel admitted produced it; it takes no path, returns
UTF-8 text with a `[truncated ... call again with offset=N]` marker past the
window, and answers one metadata line for anything that is not text.

### Channel history and its summary

A turn's prompt carries the channel's history as one JSON object per line:
`{seq, author_kind, author, role, kind, content}`, plus `artifacts`
(`{id, name, media_type, size}`) on the messages that carry files. It is
bounded to 200 messages and 60,000 characters. When anything is dropped, the
history opens with a `channel_summary` line holding the channel's latest
summary, so the earlier conversation is compacted rather than lost. The
summary is written after a turn settles -- on its own thread, not in the
turn's lane, and with a bounded wait -- by one tool-less call on the
concierge's own model (`[concierge] model`), in a session belonging to that
channel alone. It covers exactly the messages that call was shown, so a
backlog too large for one excerpt is summarised over several compactions.
The first summary is written as soon as the history trims; after that it is
rewritten once 50 messages or 20,000 characters have fallen out since, not on
every turn, so the few messages between the summary and the window wait for
the next batch. Only the newest summary is kept, and its model call is charged
to the channel. It is best effort, and a channel without a summary simply gets
a shorter history.

### What a run says in its channel

A run linked to a channel posts into it under the name of the agent doing
the work, advertised as `collaboration.run_progress`. A post is a message
with `kind` `agent_update`, `role` `assistant`, `author`
`{"kind": "agent", "id": <slug>}` and `agent_slug` set to the same slug.
It carries `post_kind`, one of `plan`, `progress`, `review`, `delivery`,
`reply` or `notice`; every other message reports `post_kind` as null. When
the run names files, they are listed on the post's `work.artifacts` in the
shape described above.

A post belongs to the turn that asked for its work, the same turn that
work's result is delivered on, and to no turn at all rather than to one
from another channel. A run a channel asked for outside any turn of its
own still posts and still names its files: `work.turn_id` is null on such
a post, so a client reads it as nullable. A snapshot a run hands in that
does not fit this shape is replaced by what sbxloop itself knows about
the work, and a snapshot already recorded that a later build cannot read
is reported as no snapshot: a message the reader cannot parse never costs
the channel its message list.

Each post names a dedupe key, which is what makes a replayed, resumed or
re-observed run post a moment once: the same key returns the message
already recorded rather than a second copy of it. A channel that was
deleted receives nothing. A silenced channel drops the running commentary
and still hears the posts that end a run: `delivery` and `notice`.

Clients read posts with the message history they already poll, or
incrementally with `GET /v1/channels/{id}/messages?after=<sequence>`, and
see each one as a `collaboration.message.created` event carrying
`post_kind` and the run's public `run_id`, the id
`GET /v1/runs/{id}` answers to. The events of a run a channel asked for, and the
run's catalogued files, belong to that channel: a member who can open it
sees them. A workspace member without `artifacts:read` may list and download
the files of a run a channel they can open asked for, through
`GET /v1/runs/{id}/artifacts` and `GET /v1/artifacts/{id}[/content]`; every
other run's files, and an id nobody catalogued, answer the same `403`
naming `artifacts:read` that they always did.

A `post_kind` sbxloop does not know is never stored: the post is dropped and
its dedupe key stays free. One recorded by a later build reads back as null.

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

Each agent has a long-term memory, reviewed and edited through
`/v1/agents/{slug}/memories` (advertised as `agents.memory`; an alias names the
same agent, and an unknown agent is `404 agent_not_found`). `GET` needs
`collaboration:read`; `POST`, `PATCH` and `DELETE` need `collaboration:write`.
A memory is `{id, agent_slug, kind, content, source_channel_id, source_run_id, source_message_id, author, pinned, created_at, updated_at, last_used_at, revision}`,
where `kind` is `fact`, `preference` or `procedure` and `author` is
`agent:<slug>` or `user:<id>`. A memory with no `source_channel_id` is global;
one learned in a channel is listed only with `?channel_id=` naming that channel
(or a channel whose `visibility` is `workspace`). `include_private=true`
lists every memory for a plain API client or a workspace owner or admin; for
any other member it adds only the memories from channels that member can read
(workspace channels and the ones they created or belong to). A member never
sees a memory from a private channel they cannot read. `q` keeps memories
sharing a word with it. Listing does not change `last_used_at`.

`POST {content, kind?, pinned?, channel_id?}` stores a memory authored by the
caller; the text is cut to `[memory] max_item_chars`, and past
`[memory] max_items_per_agent` the agent's oldest unpinned memory is dropped
(`409 memory_full` when every one is pinned). `PATCH {content?, pinned?, expected_revision}`
answers `409 revision_conflict` for a stale revision. `DELETE` forgets the
memory (a soft delete). With `[memory] enabled = false`, `POST` answers
`409 memory_disabled`. Changes write `agent.memory.created`, `.updated` and
`.deleted` events that name the memory, its agent and its source channel but
never its text.

A mentioned agent's chat persona carries the memories it may see in the turn's
channel (nothing is added when it has none). An agent whose `tools` list names
`memory`, or a person's own agent with no `tools` list, is also given
`remember`, `recall` and `forget` in chat; a read-only peer turn gets `recall`
alone. What it keeps is authored `agent:<slug>` and scoped to the channel and
message of the turn. Built-in agents and `[[agents]]` entries with no `tools`
list get no memory tools. In a run, a custom agent's memory block is taken
when the run is planned and kept across a resume, and an agent whose `tools`
names `memory` gets the same tools, writing with the run's id and channel; a
read-only session, and a critic whatever its session, gets `recall` alone, as
a read-only chat turn does. With `[memory] enabled = false` no memory reaches
a prompt and no tool is offered.

A run started from a channel keeps what its agents remember for that channel.
A run with no channel — one a labelled issue, a schedule or the CLI started —
has no channel to keep it for, so what its agents remember there is
**workspace-global**: that agent recalls it in every channel, for anyone who
can address it. This is deliberate, so a run's agent can use next week what it
learned this week wherever the next ask arrives. The `remember` tool says so
in its own description whenever the agent is working without a channel, and
`GET /v1/agents/{slug}/memories` shows such a memory with no source channel.
Give a run a channel when what its agents keep should stay in one place.

A memory's text stays out of the daemon log: a `remember` or `recall` tool
call is logged by length, not by content, as `agent.memory.*` events are
logged by id. The log is one stream for the whole installation, and any agent
can read it from any channel through `daemon_log`.

### Connections

When capability discovery includes `collaboration.connections.manage`, a
workspace owner can configure GitHub, GitLab, Slack, Discord and Mattermost
through `PUT /v1/connections/{service}`. The body has `settings` (the service's
nonsecret URL and channel fields), `credentials` (write-only tokens), and
`activate`. Only the listed fields are accepted. Secrets are written to the
home's private `config/secrets.env`; other settings go to its
`config/sbxloop.toml`. Both save paths keep timestamped backups; secret
backups remain mode `0600`. The response contains
only presence flags and nonsecret settings. Existing `POST /v1/connections`
and `PATCH /v1/connections/{id}` clients still receive
`operator_managed_connection` instead of accidentally using the old mutation
shape.

`GET /v1/connections` reports `configured` (settings and required credentials
are present), `active` (the running daemon selected that service),
`restart_required`, and `status`. A saved configuration begins as
`disconnected`: the list never calls it connected solely because a token or
channel ID exists. `POST /v1/connections/{id}/test` contacts the provider and,
for chat services, checks channel access. A successful check verifies those
requests; it does not prove that the long-lived bridge is running. A failed
check reports a generic refusal without returning provider bodies or secrets.

Changes take effect after a daemon restart. `DELETE /v1/connections/{id}`
clears credentials owned by `secrets.env`; for a chat bridge it also removes
its channel selection. For a forge it leaves repository and VCS assignments
intact, so existing repositories are never silently moved to another forge.
Secrets supplied outside the managed file must be removed by the host operator;
the API refuses to claim their removal. Gitea remains visible but unavailable
until it has an execution backend. GitHub App credentials remain host-managed;
the catalog identifies that auth method, and its check directs the operator to
`sbxloop doctor` rather than claiming a PAT check verified the App installation.

### Repository discovery

When capability discovery includes `repositories.discover`, a workspace owner
can ask `GET /v1/repositories/available` which repositories the host's forge
credential can see, so a client offers a list to pick from instead of a box
to spell `owner/name` into. `forge` defaults to `[vcs] kind`. The answer is
`{forge, credential: {mode, login}, data: [...], truncated}`: `mode` is `pat`
(a personal token, with the account it belongs to) or `app` (a GitHub App
installation; its own repository list, no login), and every entry carries
`repository`, `owner`, `name`, `private`, `archived`, `default_branch`,
`url` and `configured` (already declared to this daemon). The listing is read
from the forge now, on the host, with the same credential snapshot the
connection check uses (`GH_TOKEN` / `GITHUB_TOKEN`, else the App; the
`[vcs] token_env` variable for GitLab); it walks at most two thousand entries
and says `truncated` past that. Nothing is written. Without a credential the
route answers `409 discovery_unavailable` naming what to set; a forge that
refuses the credential is `502 provider_error` with the status and never the
body; an unreachable one is `502 provider_unreachable`. The route is the list to
choose from; registering one is the next section.

### Repositories

Where a repository is registered is the daemon's database, advertised as
`repositories.manage`. The file's `[[vcs.repos]]` entries are imported at
first sight (once; a removed registration keeps its row, so the file's copy of
that name is not imported again), and from then on `daemon:manage` clients
change the registration live:

| Route                          | Body                                              | Result                                                      |
| ------------------------------ | ------------------------------------------------- | ----------------------------------------------------------- |
| `POST /v1/repositories`        | `{repository, forge?, enabled?, deliver_base?}`   | `201 {repository, message, operation}`; `repo.add` recorded |
| `PATCH /v1/repositories/{id}`  | `{enabled?, deliver_base?}`, only the fields sent | `200`, the repository as it stands; `repo.update` recorded  |
| `DELETE /v1/repositories/{id}` | none                                              | `200 {repository: null, message, operation}`; `repo.remove` |

`repository` is `owner/name` (`group/subgroup/project` on GitLab); `forge`
defaults to `[vcs] kind`; `deliver_base: null` on a `PATCH` clears it. A name
registered already (case-insensitively), one that is not a repository on its
forge, or an unknown forge is `422 invalid_argument`; an unknown id is `404`.
Every entry of `GET /v1/repositories` now carries `source` (`config` for an
imported entry, `api`), `created_by`, `created_at` and `restart_required`.

A registration takes effect in what the daemon *admits* at once: intake,
the engine's narrowing, the concierge and this catalog all answer for it.
What the daemon *polls* was built at start, so a registration that changes
the enabled set says `restart_required` (and the reply's `message` says
so); `POST /v1/daemon/restart` applies it. The file keeps a repository's
other settings — labels, templates, workspace, sandbox packages, model
overrides — folded under the registration of the same name; a new entry in
the file is registered at the next start, and the file's `enabled` /
`deliver_base` are only the initial values (`sbxloop doctor` names an entry
the file still spells differently). The socket takes the same commands:
`repository.add` (params), `repository.update` and `repository.remove`
(target `repo_…`).

### Workspace people

A workspace holds owners, admins and members. These routes are advertised as
`users.directory` and `workspace.members`:

| Route                                    | Who          | Result                                                             |
| ---------------------------------------- | ------------ | ------------------------------------------------------------------ |
| `GET /v1/users`                          | any member   | `{data: [user]}`, oldest member first                              |
| `PATCH /v1/workspace/members/{user_id}`  | admin, owner | `{role?, is_active?}`, answers the updated `user`                  |
| `DELETE /v1/workspace/members/{user_id}` | admin, owner | `204`; the membership ends                                         |
| `POST /v1/workspace/invites`             | admin, owner | `201 {id, token, expires_at, role, email}`                         |
| `GET /v1/workspace/invites`              | admin, owner | `{data: [{id, role, email, expires_at, accepted_at, created_by}]}` |
| `DELETE /v1/workspace/invites/{id}`      | admin, owner | `204`; the invite's token admits nobody                            |

A `user` is `{id, username, email, full_name, avatar_url, role, is_active, auth_source, last_seen_at}`, where `role` is `owner`, `admin` or `member`,
`auth_source` is `local` or `oidc`, and `last_seen_at` is the last
authenticated request (recorded at most once a minute) or `null`.
`GET /v1/users/me` also carries the caller's `role`, `avatar_url` and
`auth_source`.

Rules:

- Only an owner grants the owner role, invites an owner, or changes,
  deactivates or removes an owner (`403 owner_required`).
- Nobody deactivates or removes themselves (`409 self_action`).
- The workspace always keeps an active owner (`409 last_owner`).
- A caller below admin is refused with `403 forbidden_role`. A plain API
  client with no user counts as an owner when it holds `daemon:manage`, and
  is refused with `forbidden_role` otherwise.
- An unknown user or invite is `404 user_not_found` or `404 invite_not_found`.
  An invite already spent cannot be revoked (`409 invite_accepted`).

Deactivating a user (`is_active: false`) revokes their refresh tokens, and
every access token they hold is refused at once (`401 user_inactive`), as is
their login. Reactivating restores their role's capabilities. Removing a
member leaves their client with no capability and revokes its refresh
tokens. The refresh tokens are revoked in the same database transaction as
the membership change, so either both happen or neither does.

An invite's token appears only in the creation response; the daemon keeps
its SHA-256. `ttl_hours` defaults to 72 and may be 1 to 720. An invite with
an `email` admits only a registration with that email, compared without
regard to case (`403 invite_email_mismatch`). The `email` is trimmed of
surrounding whitespace first; an empty or all-whitespace `email` is treated
as absent, so that invite admits any address. An invite's `created_by`, and
the `invited_by` of the membership it creates, is the inviting user's id, or
`client:<client id>` when a plain operator client created it.

Every change records an event without any token:
`workspace.member.updated`, `workspace.member.removed`,
`workspace.invite.created` and `workspace.invite.revoked`, each with the
acting user or client as `actor`.

### Channel access, members and participants

A channel is `private` (its channel members only) or `workspace` (every
workspace member). Channels list and read with `visibility`, `created_by`,
`silenced_until` and the caller's `my_role` (`owner`, `member`, or null when
the caller has not joined). `GET /v1/channels` lists the channels the caller
belongs to plus every workspace channel. The rules:

- A private channel the caller does not belong to answers `404 channel_not_found` on every route, exactly like an unknown id, whatever the
  caller's workspace role.
- Any workspace member may read and post to a workspace channel. Posting (a
  turn, a reaction, a participant change) makes the caller a channel member.
- Managing a channel (`PATCH` title or `visibility`, `DELETE`, adding or
  removing someone else) takes the channel's owner, or a workspace owner or
  admin who can see it; anyone else gets `403 channel_forbidden`.
- A turn may be cancelled by the person who asked or by someone who manages
  the channel.
- Teams and preferences stay per person.

When `/v1/capabilities` lists `collaboration.channel_members`:

| Route                                        | Needs  | Result                                                                                                                       |
| -------------------------------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------- |
| `GET /v1/channels/{id}/members`              | read   | `{data: [{user_id, role, joined_at, last_read_sequence, user: {id, username, full_name, avatar_url}}]}`                      |
| `POST /v1/channels/{id}/members`             | manage | Body `{user_id, role?}` (`member` by default); `201` with the entry; `200` when a role changed; `409 already_channel_member` |
| `DELETE /v1/channels/{id}/members/{user_id}` | manage | `204`; one's own id leaves the channel and needs only read                                                                   |

The user must be an active workspace member (`404 user_not_found`). Posting a
current member with an explicit `role` other than theirs changes that role in
place and answers `200`; with no `role`, or the role they already have, it is
`409 already_channel_member`. The last channel owner cannot step down (`409 last_channel_owner`), nor leave or be removed while anyone else remains: make
another member an owner first. Changes record `collaboration.member.added`,
`collaboration.member.updated` and `collaboration.member.removed` events with
`{channel_id, user_id}`.

When `/v1/capabilities` lists `collaboration.participants`, agents are channel
participants:

| Route                                          | Needs | Result                                                                                         |
| ---------------------------------------------- | ----- | ---------------------------------------------------------------------------------------------- |
| `GET /v1/channels/{id}/participants`           | read  | `{data: [{agent_slug, mode, added_by, muted_until, created_at, status, activity}]}`            |
| `PUT /v1/channels/{id}/participants/{slug}`    | post  | Body `{mode?, muted_until?}`; adds the agent (`mention` by default) or changes the fields sent |
| `DELETE /v1/channels/{id}/participants/{slug}` | post  | `204`; `404 participant_not_found` when it is not in the channel                               |

`slug` must name an enabled agent in the registry (`404 agent_not_found`).
`added_by` is an author object. `status` is `thinking` while the agent answers
a running turn in the channel, `working` while a live run linked to the
channel is credited to it (`activity` is then the run's title), and `idle`
otherwise. Mentioning an agent with `@slug`, or targeting it, adds it as a
`mention` participant when the turn is accepted. Changes record
`collaboration.participant.added`, `.updated` and `.removed` with
`{channel_id, agent_slug}`; an agent starting and finishing its part of a
turn records `collaboration.participant.activity` with `{channel_id, agent_slug, status}` (`thinking`, then `idle`; Angie reports as `concierge`).

An agent's reply is prose in the channel, so `@slug` in it addresses that
agent: a follow-up turn is accepted for it, with `trigger: "mention"`, the
replying agent as its author, the reply as its input message,
`parent_turn_id` naming the turn that produced the reply, and `chain_depth`
one deeper. Mentions inside a fenced or inline code span and inside a block
quote address nobody, an agent never addresses itself, and at most four
agents are addressed from one reply. The agent joins the channel as a
`mention` participant if it is not one already. An agent still to answer
in the same turn, whether the person asked for it or a peer handed off to
it, is not addressed again: it sees the reply in that turn.

A follow-up is a peer request, not the person's. The agent answers the
other agent's message framed as that agent speaking and as no new human
approval, with read-only tools, no MCP servers, no memory writes and no
`handoff_agent`, so one agent's prose cannot make another act on the
person's authority. `handoff_agent` itself, a peer request inside one
turn, is unchanged.

Every follow-up passes the `[collaboration]` guardrails first, and each
decision records `collaboration.followup.queued` or
`collaboration.followup.suppressed` with
`{channel_id, agent_slug, source_agent_slug, trigger, chain_depth, reason, retry_at}`
— never the message text. `reason` is `chain_depth` (past
`max_chain_depth`), `silenced` (the channel is quiet), `channel_rate` or
`agent_rate` (past `channel_turns_per_window` or `agent_turns_per_window`
inside `window_s`), `pair_cooldown` (that agent addressed this one less
than `pair_cooldown_s` ago), or the workspace budget's own reason.

With `[collaboration] ambient = true`, a participant whose `mode` is
`ambient` may also answer a message nobody addressed to it. Every message in
the channel is put through three gates in order, cheapest first: the agent's
`interests` matched case-insensitively over the last `ambient_window_messages`
(no match and no mention means nothing further happens and no model is
called); the guardrails above, with `trigger: "ambient"`, plus
`ambient_max_per_hour` for that agent in that channel; and one short
relevance call on `ambient_model` — the concierge's model when unset — that
answers RELEVANT or PASS. A PASS posts nothing and records
`collaboration.followup.suppressed` with reason `ambient_pass`; being over
the hourly cap records reason `ambient_cap`. Each decision is recorded once,
as its final outcome: `queued` only once the turn exists. What passes all
three becomes a turn with `trigger: "ambient"`, and its reply is an ordinary
agent message; the turn runs read-only with no actions and no handoff, since
nobody asked for it. The relevance call is one-shot and resumes no session.
An agent never answers its own message, an agent already answering the turn
or named in the message does not also volunteer, and each message is looked
at once, by the turn that posted it. `ambient = false`, the default, skips
all of it.

A person has the last word over all of it:

| Route                           | Needs    | Result                                                               |
| ------------------------------- | -------- | -------------------------------------------------------------------- |
| `POST /v1/channels/{id}/stop`   | delegate | `{cancelled_turns, cancelled_runs, cancelled_items, silenced_until}` |
| `POST /v1/channels/{id}/resume` | delegate | The channel, with `silenced_until` cleared                           |
| `PUT /v1/channels/{id}/silence` | delegate | Body `{until}` (a timestamp, or null to lift it); the channel        |
| `PUT /v1/channels/{id}/read`    | write    | Body `{sequence}`; the caller's channel member entry                 |

Stop cancels the channel's queued and running turns, cancels the runs its
work items are executing, abandons the work items it queued that have not
started, and silences the channel for an hour; resume lifts the silence but
restarts nothing. The runs and items are cancelled through the daemon's
control service with run control scoped to this channel's own work, so a
plain member who may post stops them too, the audit record names that
member, and nothing another channel asked for is touched. Gated work and
work awaiting review is left alone: it already waits on a person, and
dropping it would discard a finished result.
Silence quiets the agents without cancelling anything. Channel-level
permission for stop, resume and silence is **post**, not manage: a person
watching agents go somewhere they should not is the guard that matters, and
waiting for whoever owns the channel would defeat it. Features:
`collaboration.channel_stop`, `collaboration.silence`.

`PUT /v1/channels/{id}/read` records how far the caller has read. The
sequence only moves forward and never past the newest message, the members
entry carries `last_read_sequence`, and `ConversationOut` gains
`unread_count` (null for a caller with no channel membership, such as a
plain API client). Feature: `collaboration.read_state`.

### Bridge links

A channel can have a window onto a chat service: a Slack, Discord or
Mattermost surface where the same conversation happens. When
`/v1/capabilities` lists `collaboration.bridges`:

| Route                                  | Needs                   | Result                                                                                                                                                        |
| -------------------------------------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /v1/bridges`                      | read                    | `{data: [{backend, configured, label}]}` — the services this release can bridge, and whether one is set up here                                               |
| `GET /v1/channels/{id}/links`          | manage                  | `{data: [{id, channel_id, backend, surface_id, thread_id, allow_guests, created_by, created_at, active}]}`                                                    |
| `POST /v1/channels/{id}/links`         | manage, workspace admin | Body `{backend, surface_id, thread_id?, allow_guests?}`; `201` with the link; `409 link_exists` for a taken surface, `409 link_run_thread` for a run's thread |
| `DELETE /v1/channels/{id}/links/{lid}` | manage                  | `204`; `404 link_not_found`                                                                                                                                   |

Creating a link takes managing the channel and being a workspace owner or
admin (`403 channel_forbidden` otherwise): a link makes the channel hear
everyone on that surface and post its own traffic there, which reaches
past the channel itself. A thread a run opened is refused. A Discord thread
is a channel of its own, so a Discord link given a `thread_id` is stored
with that thread as its `surface_id` and no `thread_id`; Slack and
Mattermost keep both. Deleting a link and linking the same surface or
thread again works.

While a surface is linked, what people type there becomes a turn in the
channel it mirrors, instead of reaching the daemon's concierge. A link is a
window on a channel, not a grant of operator powers: it never widens where
`!sbx` runs, so on a linked surface that is not the control channel the one
command is `!sbx link`, and every other is refused with a note saying where
it does run. Commands on the control channel, run-thread steering and an
unlinked surface behave exactly as they did. Every message appended to the
channel — a person's, an agent's, a run's delivery, a failed turn's error,
one agent's request to another — is posted back to each linked surface
under a `**name**` header, except to the surface it arrived on, so two
linked services mirror each other without a loop.

A message that arrived over a bridge carries `origin`:

```json
{ "backend": "discord", "surface_id": "C123", "external_message_id": "998" }
```

Angie shows it as a "via" badge; it is `null` for everything typed here.

Who somebody is on a bridge is theirs to prove, once:

| Route                                      | Needs | Result                                                                     |
| ------------------------------------------ | ----- | -------------------------------------------------------------------------- |
| `POST /v1/users/me/identities/link-code`   | write | `{code, expires_at}` — shown here and nowhere else, single use, 10 minutes |
| `GET /v1/users/me/identities`              | read  | `{data: [{backend, external_user_id, display_name, verified_at}]}`         |
| `DELETE /v1/users/me/identities/{backend}` | write | `204`; `404 identity_not_found`                                            |

The person types `!sbx link <code>` on the bridge, from the account they
want mapped. A message from an author nobody has mapped is refused with a
short reply pointing at that command — unless the link was created with
`allow_guests`, in which case it is stored as a person with no account,
under the name they use on that service. A map is only as good as the
membership behind it: an account removed from the workspace or deactivated
is unmapped again, and the link's `allow_guests` rule decides afresh.

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
| `GET`    | `/v1/auth/providers`                         | none                   | The sign-ins a signed-out client may offer                            |
| `POST`   | `/v1/auth/oidc/token`                        | none                   | Redeem an OpenID Connect authorization code for a token pair          |
| `GET`    | `/v1/users/me`, `/v1/agents[/{slug}]`        | collaboration read     | Local profile and product agent catalog                               |
| `GET`    | `/v1/users`                                  | workspace member       | The workspace directory                                               |
| `PATCH`  | `/v1/workspace/members/{user_id}`            | workspace admin        | Change a role; deactivate or reactivate a user                        |
| `DELETE` | `/v1/workspace/members/{user_id}`            | workspace admin        | End a membership                                                      |
| `POST`   | `/v1/workspace/invites`                      | workspace admin        | Create an invite; the raw token appears only here                     |
| `GET`    | `/v1/workspace/invites`                      | workspace admin        | List invites                                                          |
| `DELETE` | `/v1/workspace/invites/{id}`                 | workspace admin        | Withdraw an invite                                                    |
| `POST`   | `/v1/agents`, `/v1/agents/{slug}/archive`    | collaboration write    | Save a person's own agent; archive it                                 |
| `PATCH`  | `/v1/agents/{slug}`                          | collaboration write    | Edit a saved agent at the revision last read                          |
| CRUD     | `/v1/teams`, `/v1/channels`, `/v1/workflows` | collaboration          | Local teams, durable conversations, and workflow definitions          |
| CRUD     | `/v1/agents/{slug}/memories[/{id}]`          | collaboration          | An agent's long-term memory, scoped by source channel                 |
| `GET`    | `/v1/channels/{id}/messages`                 | collaboration read     | Immutable ordered conversation history                                |
| `POST`   | `/v1/channels/{id}/turns`                    | collaboration delegate | Accept an idempotent conversation/delegation turn                     |
| CRUD     | `/v1/channels/{id}/members`, `/participants` | collaboration          | The people and agents in a channel                                    |
| `GET`    | `/v1/bridges`                                | collaboration read     | The chat services a channel can be linked to                          |
| CRUD     | `/v1/channels/{id}/links`                    | collaboration          | The bridge surfaces mirroring a channel                               |
| CRUD     | `/v1/users/me/identities[/{backend}]`        | collaboration          | Who you are on a bridge, and the code that proves it                  |
| CRUD     | `/v1/prompts`, `/v1/connections`             | collaboration          | User preferences; redacted connections and owner management           |
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
| `GET`    | `/v1/usage/pool`                             | `runs:read`            | Today's runs and tokens against the daily cap and budget              |
| `GET`    | `/v1/operations[/{id}]`                      | `audit:read`           | Every command any surface recorded                                    |
| `GET`    | `/v1/repositories`, `/profiles`, `/recipes`  | `runs:read`            | What work may be admitted against                                     |
| `POST`   | `/v1/repositories/{id}/resume`               | `daemon:manage`        | Poll a suspended repository again                                     |
| `GET`    | `/v1/repositories/available`                 | owner role             | What the host's forge credential can see, to pick one to register     |
| `POST`   | `/v1/repositories`                           | `daemon:manage`        | Register a repository; polled from the next start                     |
| `PATCH`  | `/v1/repositories/{id}`                      | `daemon:manage`        | Enable, disable or re-base a registered repository                    |
| `DELETE` | `/v1/repositories/{id}`                      | `daemon:manage`        | Forget a registration; queued and running work is untouched           |
| `GET`    | `/v1/daemon/holds`                           | `runs:read`            | Standing holds and whose they are                                     |
| `POST`   | `/v1/daemon/holds`                           | `daemon:manage`        | Take a hold attributed to this client                                 |
| `DELETE` | `/v1/daemon/holds/{name}`                    | `daemon:manage`        | Release your hold; `?force=true` overrides another's                  |
| `POST`   | `/v1/daemon/stop`, `/v1/daemon/restart`      | `daemon:manage`        | Graceful stop; a stop the supervisor undoes                           |
| `GET`    | `/v1/schedules[/{name}]`                     | `runs:read`            | Schedules with cadence, last and next due                             |
| `PATCH`  | `/v1/schedules/{name}`                       | `daemon:manage`        | Atomically replace or rename a schedule while preserving run history  |
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

### Steering one task, or one agent

`POST /v1/runs/{id}/steering` takes two optional targets beside `text`.

- `task_id` addresses one task lane. The instruction waits in that task's
  own mailbox and is answered when *that* task reaches a phase boundary,
  which is what makes steering meaningful with `[budgets] max_parallel_tasks` above 1: without it the lane that answers is
  whichever one got to a boundary first. A task that finishes with
  instructions still waiting hands them to the run, where they are answered
  as run-level direction rather than dropped.
- `agent_slug` names an agent on the run's assignment. The answer comes
  back in that agent's persona and with its model, and the run's
  `chat.reply` event carries `agent_slug` so a reader can attribute it.

Both are optional and neither is required to exist: an instruction that
names no target, or a target this run does not have, is answered exactly as
it always was. A daemon that does not list `collaboration.mention_steering`
ignores both fields, so sending them is safe against an older server.

In a channel, `collaboration.mention_steering` means a mention of an agent
already working live work there is taken as direction for that run instead
of starting a fresh answer: the turn's `steered_run_id` names the run. The
mention has to be unambiguous -- one live run in the channel with that agent
on it -- or it stays an ordinary turn. Stopping stays explicit: `/stop`,
`/cancel`, or exactly `@agent stop` cancels the channel's runs (that agent's
alone, for the third), through the same cancel the API's
`POST /v1/runs/{id}/cancel` uses. A message that merely argues for stopping
is steering, not a stop.

Both act as the person who wrote the message. A steer takes the
capabilities their workspace role grants (`runs:steer`, which a `member`
holds). A stop takes the rule `POST /v1/channels/{id}/stop` takes: anyone
who may post in the channel may stop the runs that channel asked for,
without `runs:control`, so a plain `member` may stop as well as steer. The
cancel is recorded in the person's name and reaches only that channel's
runs; someone who may not post there is told nothing was stopped. Only a
message the person wrote steers or stops: a turn another agent started
never does, and an agent reached through another agent's handoff answers
the request it was handed.

### Who sees which events

When `/v1/capabilities` lists `events.scoped`, every event is recorded with
the channel it belongs to (its own channel, the channel a memory was learned
in, or the channel that asked for its run or item) and, for a person's own
teams, preferences, workflows and profile, the one user it is for. The page routes, the run's events, the SSE
stream and the WebSocket all filter in the query, so a page is never short
and `has_more` means what it always meant:

- A plain API client (no workspace member behind it) sees every event.
- Every workspace member sees events meant for everyone or for them alone.
- A workspace owner or admin also sees every channel's and every run's
  events.
- A plain member sees the events of the channels they can open (their
  channels and every workspace channel) and events with neither a channel
  nor a run. A run's events are shown only when a channel they can open
  asked for the run (a workload or tool run started from a chat message, or
  a code run whose issue a chat turn filed); a run no channel asked for, and
  run events recorded before this release, are not shown to them.

A live subscription moves its cursor past events its member may not see,
so it does not scan them again. Membership changes apply from the next
read (the stream and the socket re-read the member when they re-check the
token). `GET /v1/events` and `GET /v1/events/stream` accept
`channel_id=<chn_...>` to follow one channel.

## Errors

Every refusal is `application/problem+json` with a stable `code`, the
request's `X-Request-Id`, and the fields a client needs to act:

| Status | Codes                                                                                                                                                                                                                                                                                                                                        |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 400    | `invalid_request`, `invalid_cursor`, `oidc_invalid_request`                                                                                                                                                                                                                                                                                  |
| 401    | `unauthenticated`, `invalid_token`, `token_expired`, `token_revoked`, `client_revoked`, `refresh_reuse_detected`, `oidc_exchange_failed`                                                                                                                                                                                                     |
| 403    | `forbidden` (with `capability`), `oidc_not_allowed`, `oidc_account_disabled`, `oidc_not_provisioned`                                                                                                                                                                                                                                         |
| 404    | `not_found`, `unknown_target`, `agent_not_found`                                                                                                                                                                                                                                                                                             |
| 409    | `not_eligible`, `already_terminal`, `already_in_progress`, `stale_revision`, `unsupported_for_kind`, `capability_unknown`, `capability_unsupported`, `idempotency_conflict`, `hold_owned`, `unsupervised`, `agent_read_only`, `agent_revision_conflict` (with `current_revision`), `agent_exists`, `agent_archived`, `oidc_account_conflict` |
| 410    | `cursor_expired` (with `snapshot`), `artifact_gone`                                                                                                                                                                                                                                                                                          |
| 411    | `length_required`                                                                                                                                                                                                                                                                                                                            |
| 413    | `body_too_large` (with `limit`)                                                                                                                                                                                                                                                                                                              |
| 422    | `invalid_request` (with `errors`), `invalid_argument`, `idempotency_key_required`, `unknown_action`, `invalid_agent` (with `problems`)                                                                                                                                                                                                       |
| 429    | `too_many_attempts`, `too_many_streams`                                                                                                                                                                                                                                                                                                      |
| 500    | `internal_error` (never the exception's text)                                                                                                                                                                                                                                                                                                |
| 503    | `daemon_not_ready` (with `Retry-After`), `daemon_stopping`, `source_unavailable`, `oidc_unavailable`                                                                                                                                                                                                                                         |

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
  conformance probe asks, from inside a scratch sandbox, for the API's
  `/health/live` answer on `[api] port` at the guest's loopback,
  `host.docker.internal`, its default gateway and the `[api] bind` address,
  both directly and through the sandbox's proxy, and asks the network
  policy about those addresses. Only the API's own answer counts as
  reachable: sbx accepts connections its policy then closes unanswered, so
  an opened connection proves nothing. Anything but `unreachable` fails the
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

By design, on this API: general configuration writes, backup and restore,
garbage collection, sandbox deletion, and starting a daemon that is not
running. Each stays on the host's own CLI until it has its own attribution,
conflict and active-run story. Repository registration has one (see
Repositories above); a repository's other settings are still the file's. A tool run takes no
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
