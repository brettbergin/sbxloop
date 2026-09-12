# OpenAI-compatible endpoint backend

## Scope

Add `backend = "openai"` under `[agent]` as a fourth agent backend, preserving
the Copilot default and the existing worker protocol. The same selection
serves code phases, workload phases and the chat concierge. The operator
names the endpoint under `[agent.openai]` — a self-hosted server, a gateway,
or the hosted API — and supplies the credential in the variable `api_key_env`
names; repository credentials continue to live only in the GitHub or service
sandbox.

The implementation uses the official `openai` Python client, pinned to a
floor rather than a version: only its long-stable surface is used (chat
completions with tools and streaming, the models listing). No vendor CLI,
runtime or Node dependency is part of this backend.

## Design and implementation plan

1. Make a backend's credential host and egress a question of config rather
   than a constant on the descriptor, so every host-side consumer — the
   agent sandbox's allowlist, the sbx custom-secret binding, doctor's rows,
   `sbxloop secrets` — asks with a loaded config in hand.
2. Add `[agent.openai]` (`base_url`, `api_key_env`, `request_timeout_s`,
   `max_retries`, `allow_insecure_endpoint`) with a per-repository override
   of the endpoint, one parser for the endpoint's host that both the policy
   path and the secret path read, and provisioning that allows the host,
   binds the credential to it, and routes the worker to it.
3. Adapt the `openai` client to `AgentBackend.run_session`: build the
   messages, offer the governed tools, and own the loop — call, dispatch
   every tool call, append the results, call again — until the model
   answers. Persist the transcript beside the sandbox's home as the
   session, since the wire has no server-side thread.
4. Teach doctor, `list-models` and the model catalog about the configured
   endpoint: list from it, probe it from the host, key the cache by it.

The design constraint that shapes all of it: **the `openai` client is a
client, not an agent harness.** The other three backends hand the model
loop, tool execution and session state to a vendor SDK. This one gives the
worker one completion call and nothing else, so the worker owns the loop —
and owns no new tools for it. It drives the governed layer that already
exists (the worker-owned local tools and the copilot backend's governor,
registry and health tracker, imported across module lines exactly as the
codex backend does), so read-only sessions, per-phase tool ceilings and the
host-tool round trip behave identically on every backend.

### The endpoint is operator config, never plan-declared egress

A model endpoint is infrastructure, not task egress. It is allowed on the
agent sandbox at provision time from `[agent.openai]`, and the credential is
bound to its host there; it is never granted late from a plan's `egress`
declaration. That is why the parser for an operator-configured endpoint
accepts a single-label hostname, an IPv4 or IPv6 literal and an explicit
port — shapes a served endpoint usually has — while the rule for what a
*plan* may declare is unchanged: a plan naming a bare host or a literal is
still refused with the same message as before.

Whether sbx's `policy allow network` and `secret set-custom` accept a
single-label host or an address literal is **field-unverified**. The
allowlist entry is the bare host (a policy that cannot narrow to a port
allows the host), and when sbx refuses the allow batch or the binding that
carries the endpoint, provisioning stops naming the endpoint and its shape
— never a sandbox left to fail at its first model call with a bare
connection error.

A plain `http://` endpoint refuses to load unless `allow_insecure_endpoint`
is set: a credential bound to it travels in cleartext, which is never done
by accident. An endpoint that wants no credential still needs a placeholder
value in the variable, because sbx registers a binding either way.

### Governed tools

The backend offers the chat-completions tool shape — `function` with a JSON
Schema under `parameters` — for the worker's own read/list/search/write/
shell tools (the read-only set in read-only sessions; none when the job's
allowlist is empty, as for the concierge) plus the job's host tools. The
model's `tool_calls` come back with `arguments` as a JSON *string*;
malformed arguments are a tool failure the governor records and the model
reads, never an exception that ends the session. Every call passes the
tool-call governor before anything runs; the ceiling ends the turn with the
same in-session nudge the other backends send. Host tools keep the
event/file relay.

There is no native MCP: a job carrying `mcp_servers` is refused by name.
Credentialed HTTP MCP servers work through sbxloop's host mediation, as
they do under codex.

### Authentication and session state

Where the calls go is the sandbox's environment, delivered by provisioning:
`OPENAI_BASE_URL` names the endpoint, `SBXLOOP_OPENAI_API_KEY_ENV` names
the variable holding the credential, and the client's timeout and retry
count ride beside them. The credential itself rides the secret path under
that variable's name and never appears in `sbx` argv or an event; its value
is replaced in everything the backend emits.

Chat completions is stateless. The backend mints a session id and persists
the transcript beside the sandbox's home, bound to a fingerprint of the
tool roster, permission mode, instructions, workspace, model and endpoint.
`resume_session_id` continues from it; a changed fingerprint starts fresh;
a `require_resume` job whose transcript is missing or unmatched fails
closed with a named recovery failure rather than replaying tools in a fresh
session. A transcript lives as long as the sandbox does: a resume after a
fresh provision starts fresh, as it does under codex.

`model = "auto"` takes the first model the endpoint lists — the one a
single-model box serves. An endpoint that lists nothing cannot pick, and
says so rather than guessing a name.

### Chronology and human control

The backend emits the same events as the others, attributed to `openai`:
`agent.message` with the model and backend, streamed deltas so the status
line moves during a long local generation, tool start and end through the
registry, denials and failures through the health tracker, usage per call.
Token counts come from the response's own usage; no monetary estimate is
invented, because a served endpoint has no price. The capacity query is
unsupported, as it is for codex.

Failures name the endpoint and the model, never the key: a refused
connection, a 404 on the base URL or the model, a refused credential (by
its variable's name), a throttle with its retry hint, a server error. An
`expect="json"` reply that will not parse gets one reask and then the
runner's own missing-JSON error — never an unbounded retry.

Steering, hold and resume retain sbxloop's existing phase/checkpoint
behaviour. The code-run trail fixture is unchanged across the whole change.

## Verification

Config, addressing and provisioning are tested against the fake sbx: each
endpoint shape through the policy path and the secret path, the plan-egress
rule unchanged, the refused allow and the refused binding failing closed by
name. The worker loop is tested against a stub speaking the chunk shape —
the tool round trip, streaming, malformed arguments, the JSON reask,
resume with and without a transcript, each failure category — and the
published client against a loopback server in a dedicated CI job. Doctor
and `list-models` are tested for an endpoint serving a listing, one
404ing it and one unreachable from the host. The complete fast suite,
formatting, lint, typing, security and self-reference checks are the local
gates.

**Field-unverified:** a real served endpoint's behaviour (streaming with
usage in the final chunk, a server that rejects `stream_options`, the
models listing), sbx's handling of a single-label host or an address
literal in a policy allow or a secret binding, and whether the agent
sandbox can reach a private endpoint the host can. Sandbox verification
belongs on CI runners under the repository's standing policy; the stub and
the loopback server do not establish those claims. Doctor's endpoint row
says exactly what it checked — that the endpoint answers from the host —
and that the sandbox's route is a separate question.

## Sources

- [OpenAI Python client](https://github.com/openai/openai-python) and the
  chat completions API reference (tools, streaming, `stream_options`).
- Servers cited as examples of the wire shape, never as a supported-vendor
  list: vLLM, SGLang, TGI, llama.cpp and LiteLLM all serve
  `/v1/chat/completions` and `/v1/models` in this shape.
- [The Codex backend design](codex-backend.md), whose governed-tool layer
  this backend reuses.
