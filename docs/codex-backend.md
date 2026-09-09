# Codex agent backend

## Scope

Add `backend = "codex"` under `[agent]` as a third agent backend, preserving
the Copilot default and the existing worker protocol. The same selection
serves code phases, workload phases and the chat concierge. The operator
supplies `OPENAI_API_KEY`; repository credentials continue to live only in
the GitHub or service sandbox.

The implementation uses the official Python `openai-codex` SDK, pinned to
`0.147.0`. Its distribution depends on the matching
`openai-codex-cli-bin==0.147.0`, so provisioning needs only the worker's
`codex` extra. Node and the TypeScript SDK are not part of this backend.

## Design and implementation plan

1. Add the backend descriptor, configuration selection, credential
   registration, inference egress, diagnostics and model listing. Update
   examples, attribution and credential redaction alongside them.
2. Adapt the Python SDK's `CodexClient` to `AgentBackend.run_session`:
   authenticate, start or resume a thread, submit a turn, stream normalized
   events and return the authoritative result. Preserve one deadline and
   close the runtime on success, failure or timeout.
3. Expose worker-controlled dynamic tools, apply the permission and
   tool-call governor before dispatch, and relay host tools through the
   existing request event and response file. Verify behavior with fake SDK
   sessions and the repository's complete checks.

`CodexClient` uses JSON-RPC over the child runtime's stdin/stdout. This
creates no network listener or VM-to-host connection. Host calls remain
events the host already tails, answered only by files the host copies in.

### Governed tools

The convenience SDK session interface does not expose the full sbxloop
permission contract. Approval callbacks alone cover escalation requests,
not every tool invocation. Codex hooks also have paths that continue after
a hook error, and hosted tools do not all use the hook path. Neither is a
complete pre-execution tool-call governor.

Instead, the adapter disables native environment tools with the
experimental `environments=[]` field and disables other autonomous tool
features. It registers worker-owned dynamic tools through the experimental
`dynamicTools` field. The SDK/runtime pin is intentional: updates require
rechecking these controls before changing the pin.

Auto sessions receive shell, file read/write, directory listing and text
search tools. Read-only sessions receive read/list/search tools only;
unknown calls are refused. The concierge's `available_tools=[]` removes
local tools entirely. All calls, including host tools, pass the shared
tool-call governor before any command or request runs. Models may compose
these calls through Codex Code Mode: its isolated JavaScript runtime has
no filesystem, network, process or import APIs. The ceiling counts each
nested local or host tool call; pure computation and response formatting
do not consume tool calls. This is a curated tool surface rather than the
complete interactive Codex tool catalog.

The VM remains the execution boundary. Shell commands execute inside the
agent VM, where the only delivered credential is the inference key.
Reviewers inspect the existing evidence and files; mechanical verification
continues through sbxloop's separate shell-check jobs.

### Authentication and session state

Initial support is OpenAI API-key authentication. Codex app login sessions,
ChatGPT subscription credentials, external model providers and interactive
login flows are outside this change. `model = "auto"` delegates model
selection to Codex; no model migration is implied.

The adapter isolates Codex configuration and session state from the target
checkout. The supplied API key is sent through the SDK login RPC using an
ephemeral credential store, never a command argument. Project settings,
automatic instruction discovery and plugin loading cannot replace the
worker's controls. The host already supplies repository conventions in
its bounded prompt block.

Code jobs append their system message to the coding preset. Workload jobs
with `system_preset=false` replace that preset with their supplied base
instructions. Session handles bind the SDK thread to its tool definitions,
permission mode, instructions and workspace. Changing any of those starts
a fresh thread because the SDK cannot replace dynamic tools on resume.
Resume is an optimization: a missing session can start fresh before a turn
begins; failures after a turn starts are not retried as fresh work.

### Chronology and human control

The adapter emits the same message, tool, usage, permission and tool-cap
events as other backends, attributed to `codex`. Token usage is taken from
SDK counters; no monetary estimate is invented. Tool results preserve
success/failure and bounded output for the existing chat renderers.

Steering, hold and resume retain sbxloop's existing phase/checkpoint
behavior. This change does not add mid-turn steering or a new run state
machine. The code-run trail fixture must remain unchanged.

## Verification

Regression tests first establish that configuration, attribution, OpenAI
key recognition and redaction are missing. Adapter and tool tests cover
successful and failed turns, permissions, budgets, host calls, resume,
deadlines and result normalization. The complete fast suite, formatting,
lint, typing, security and self-reference checks are the local gates. A
separate CI job installs the pinned SDK and exercises its actual stdio
client against a fake runtime, covering typed notifications, tool callbacks,
resume and subprocess cleanup on login/turn timeouts without inference.

**Field-unverified:** a real Codex inference turn, model entitlement,
Docker Sandbox egress/secret injection and behavior of the pinned runtime
inside a real agent VM. Sandbox verification belongs on CI runners under
the repository's standing policy; fake sessions do not establish those
claims. A live CI run should cover a code change, a read-only review, a
host-tool call, a workload, a cap refusal and resume before production use.

## Sources

- [Official Codex SDK documentation](https://learn.chatgpt.com/docs/codex-sdk)
- [App-server protocol and dynamic tools](https://learn.chatgpt.com/docs/app-server)
- [Hooks and tool coverage](https://learn.chatgpt.com/docs/hooks)
- Published `openai-codex==0.147.0` Python package and its generated models;
  corresponding Codex runtime source inspected during implementation.
