# sbxloop user guide

The full reference for setting up, running, and operating sbxloop.
For a first run, start with the [README](../README.md).

- [Quickstart](#quickstart)
- [Agent backends](#agent-backends-copilot-claude-codex-or-an-openai-compatible-endpoint)
- [How a run works](#how-a-run-works)
- [CLI reference](#cli-reference)
- [Network access](#network-egress-least-privilege-by-plan)
- [Working with a checkout](#working-against-an-existing-checkout)
- [Daemon and chat](#the-daemon-an-always-on-outer-loop)
- [The remote API](#the-remote-api) ([reference](api.md))
- [Artifacts](#artifacts)
- [GitHub integration](#github-integration)
- [Troubleshooting](#debugging-failed-runs)
- [Toolchains](#language-toolchains)
- [Sandbox cleanup](#sandbox-hygiene)
- [Setup and diagnostics](#setup)
- [Configuration reference](#configuration)

## The primitive: a sandbox pair

Every run gets an isolated microVM agent sandbox — plus, when the GitHub integration is configured, a second github-ops sandbox, so no single environment ever holds both credentials:

| Sandbox                 | Credential                                                                                                                                                                                                                                                                                                           | Purpose                                                                                                                                                                                                                                                              |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sbxloop-<run>-agent`   | `COPILOT_GITHUB_TOKEN` (fine-grained PAT, *Copilot Requests* permission), `ANTHROPIC_API_KEY` for Claude, `OPENAI_API_KEY` for Codex, or the variable `[agent.openai] api_key_env` names for an OpenAI-compatible endpoint ([Agent backends](#agent-backends-copilot-claude-codex-or-an-openai-compatible-endpoint)) | Runs the configured agent SDK — [GitHub Copilot SDK](https://github.com/github/copilot-sdk) by default, the Claude Agent SDK, the Python Codex SDK, or the `openai` client against the endpoint you name. All model calls and tool executions happen inside this VM. |
| `sbxloop-<run>-github`  | `GH_TOKEN` (fine-grained PAT with the permissions in [docs/permissions.md](permissions.md)) — or a GitHub App installation token, host-minted and auto-refreshed ([GitHub App auth](#github-app-auth))                                                                                                               | Performs the GitHub operations (branch, PR, review, CI polling, merge, issue labels) against the one configured repository. Only provisioned when `[github] repo` is set.                                                                                            |
| `sbxloop-<run>-service` | The `[[credentials]]` a run was granted by name — operator secrets, each bound to one host (#765)                                                                                                                                                                                                                    | Makes the authenticated requests the agent asks for through its `call_service` tool, one fixed `service.http` op at a time, redacting the credential from what comes back. Only provisioned for a run granted a credential; none is today.                           |

The rule behind the table: **the only key in an agent sandbox is its inference
key.** Everything else that needs a secret happens in a separate sandbox that
runs no model and only the fixed ops the host submits; the agent dispatches
those ops and reads their results, never the credential.

Both sandboxes run under sbx's **balanced network policy** (default-deny
egress plus a curated allowlist), and tokens are injected through sbx's secret
proxy — **credential values never enter the VM**; the host proxy substitutes
them only on egress to their declared domains (where sbx's proxy cannot feed
exec'd workers, tokens are piped into each worker job over stdin — nothing at
rest in the VM — with a 0600 in-VM env file as the last-resort fallback; see
docs/architecture.md). To be honest about it: on current sbx the cached
exec-visibility verdict is negative, so that non-proxy / env-file fallback is the
**common case**, not an edge case — `proxy` names the strategy that is
*attempted*, not the one that usually runs (tracked operationally in #46;
interim hardening proposed in #592). Sandboxes are cattle: they are
torn down at run end and re-provisioned on resume, while all durable state
(workspace, SQLite checkpoints, event log) lives on the host.

### Sandbox CPU and memory

Every new VM receives explicit CPU and memory limits. These defaults are
live in the `sbxloop.toml` written by `sbxloop init`, and also apply to
existing configuration files that omit the keys:

```toml
[sandbox]
cpus = 6
memory = "12g"
concierge_cpus = 2
concierge_memory = "4g"
github_cpus = 1
github_memory = "2g"
service_cpus = 1
service_memory = "2g"
```

The run agent uses `cpus`/`memory` for code, workload and tool runs; bake
uses the same allocation. Each GitHub helper uses `github_*`, each
credential/dependency helper uses `service_*`, and the long-lived chat
concierge uses `concierge_*`. Diagnostic scratch VMs use `service_*`.
An operator's `[[github.repos]]` entry can override `cpus` and/or `memory`
for that repository's run agent. A repository-carried config cannot raise
these operator limits.

CPUs must be positive integers: `0` (all host CPUs) is refused. Memory
accepts positive whole MiB (`m`) or GiB (`g`), case-insensitively; `2048m`
and `2g` are equivalent. Empty, automatic and percentage limits are refused.
There is no fallback to uncapped creation if sbx rejects the flags. See
[the sbx create reference](https://docs.docker.com/reference/cli/sbx/create/).

These are **per-VM allocations**, not an aggregate budget or a reservation
of specific host cores. Account for every concurrent VM and other host
processes; smaller hosts need smaller settings. Larger test suites may need
larger repository overrides. `[limits] mem_warn`/`mem_abort` still monitor
pressure inside the VM and do not set its size. `sbxloop doctor` shows
requested allocations, and provisioning events include CPU and memory.

Resumes use the current operator CPU/memory settings even when the run's
other rules come from its saved configuration. Limits take effect at creation. A host receipt under
`state/sandbox-allocations/`, tied to a nonce in the VM, checks reuse across
daemon restarts and provider recovery. A pre-cutover VM, changed allocation,
missing receipt or failed identity check refuses reuse before modifying
that VM. This preserves in-VM work and concierge session history, but it
does not resize or stop the old VM. Stop the run/daemon, copy any needed
in-VM work and session files out with `sbx cp`, remove the named VM with
`sbx rm`, and resume/restart to create it with the new limits. Use the same
`sbx --app-name` when configured. Allocation receipts describe successful
creation requests, not measured runtime enforcement.

### Agent backends: Copilot, Claude, Codex or an OpenAI-compatible endpoint

The SDK that runs the agent personas is configurable (#533) — Copilot stays
the default with unchanged behaviour:

```toml
# sbxloop.toml
[agent]
backend = "claude"   # default: "copilot"
```

| backend   | host credential (agent sandbox only)                                                                                  | in-sandbox runtime                                                                                                                                                                             |
| --------- | --------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `copilot` | `COPILOT_GITHUB_TOKEN` (PAT, *Copilot Requests*)                                                                      | `github-copilot-sdk` (worker `[copilot]` extra; wheels bundle the Copilot CLI)                                                                                                                 |
| `claude`  | `ANTHROPIC_API_KEY` ([console](https://console.anthropic.com/settings/keys))                                          | `claude-agent-sdk` (worker `[claude]` extra) + the Claude Code CLI, which provisioning installs (Node + `@anthropic-ai/claude-code`)                                                           |
| `codex`   | `OPENAI_API_KEY` ([API keys](https://platform.openai.com/api-keys))                                                   | Python `openai-codex==0.147.0` (worker `[codex]` extra), including its matching Codex CLI runtime and Code Mode helper; no Node dependency                                                     |
| `openai`  | the variable `[agent.openai] api_key_env` names (`OPENAI_API_KEY` by default), bound to the endpoint `base_url` names | Python `openai` client (worker `[openai]` extra) driving `/v1/chat/completions` on a self-hosted server, a gateway or the hosted API, with the worker's own governed tools; no Node dependency |

The run protocol is shared across backends: the top-level `model` key supplies
the default model (`"auto"` lets the backend pick). Each agent can override it
independently, while all agents use the same backend. Token
usage is reported through the same `run_usage`/`usage_today` accounting, and
the credential split holds — the agent sandbox carries the chosen agent
credential and never a GitHub token. With the claude backend, provisioning
also allows `api.anthropic.com` egress and keeps the CLI hermetic
(telemetry/auto-update traffic disabled). Missing or invalid configuration
fails fast: an unknown backend fails config loading, and a missing
`ANTHROPIC_API_KEY` fails before any microVM boots. Re-run `sbxloop bake`
after switching backends so a baked template carries the right runtime; the
daemon's long-lived concierge sandbox rebuilds itself, since its reuse check
asks whether the box is equipped for the configured backend and not only
whether the worker version matches. Every host-side command that is *about*
the backend reads one descriptor (`sbxloop.backends`, #617): `sbxloop doctor` checks the configured credential and network hosts (and skips the
Copilot-SDK rows under Claude or Codex), `sbxloop secrets list|clean|rotate` manage
that credential's registration, `sbxloop list-models` lists that backend's
models, and `--model` help says so.

Ask the concierge "How much agent capacity remains?" or "When do our limits
reset?" to invoke `agent_rate_limits`. It uses the daemon's configured
backend and existing agent-sandbox credential, with no credentials in chat.
The report names its observation time, provider source, freshness and shared
account/organization scope. Unknown values stay unknown; `run_usage` and
`usage_today` continue to report recorded token spend, not provider capacity.

- **Copilot:** the documented SDK `account.getQuota` RPC reports account
  quota snapshots, including entitlement, used requests, remaining percentage,
  overage permissions and reset time where supplied. Zero remaining quota
  does not itself prove requests are blocked. Short-term request/token limits, window
  length and provider snapshot time are not exposed. An elapsed reset marks
  the report stale; a successful query alone does not prove snapshot freshness.
- **Claude:** the read-only organization Rate Limits API reports configured
  ceilings and model groups. It requires admin scope, so a configured
  workspace API key returns an explicit unsupported-permission report.
  Organization ceilings do not include lower workspace overrides and do not
  establish remaining capacity, reset times or longer-term usage quotas.
- **Codex:** this adapter currently reports the query as unsupported.

Queries never generate model responses or change billing, credentials,
models or scheduling. Authentication failures, throttling, timeouts, stale
data and missing data are explicit. A query failure preserves the concierge
sandbox and conversation. No new configuration is required.

To use Codex, set `[agent] backend = "codex"` and put `OPENAI_API_KEY` in
the home's secrets file. Provisioning installs the Python SDK in the
agent sandbox and allows `api.openai.com`. Code phases, workloads and the
concierge use the same worker protocol and chronology.

Codex uses worker-controlled shell/file tools and host tools so the
tool-call budget is checked before execution. Its read-only reviewers get
read/list/search tools; the concierge gets only its host tools. Models can
compose these calls through Code Mode's isolated JavaScript runtime. The
budget counts each nested local or host tool call; pure computation and
response formatting do not consume tool calls.

Native Codex MCP server specifications are rejected. Credentialed
Streamable HTTP MCP servers configured through `[[mcp]]` work through
sbxloop's host mediation, keeping their credentials in the service sandbox.
Credential-free native MCP servers require the Copilot or Claude backend.
The interactive Codex tool catalog, plugins and subscription login are not
enabled. The SDK/runtime pair is pinned because dynamic tool registration
and native tool suppression use experimental protocol fields. See the
[design and implementation plan](codex-backend.md) for the contract and
the **field-unverified** live-sandbox checks.

#### Pointing sbxloop at a self-hosted endpoint

An operator who serves models themselves — vLLM, SGLang, TGI, llama.cpp,
LiteLLM, or any other gateway speaking the OpenAI wire shape — or who wants
the hosted API without a vendor harness, names the endpoint and selects the
`openai` backend:

```toml
# sbxloop.toml
[agent]
backend = "openai"

[agent.openai]
base_url = "http://vllm.internal:8000/v1"   # the OpenAI-compatible root, version path included
allow_insecure_endpoint = true              # plain http:// refuses to load without this
# api_key_env = "OPENAI_API_KEY"            # the variable holding the key, never the key
# request_timeout_s = 600                   # client patience, sized for a slow local box
# max_retries = 2
```

The credential goes in the home's secrets file under whatever
`api_key_env` names — a placeholder value if the endpoint wants none, since
sbx binds the variable to the endpoint's host either way. Provisioning
allows that host on the agent sandbox, binds the credential to it, and
routes the worker to the URL; the key rides the secret path and never
`sbx` argv. A hostname on your network, an IPv4 or IPv6 literal and an
explicit port are all accepted here, and only here: a plan still declares
egress as dotted domains, so the endpoint widens nothing a plan may ask
for. A repository whose code must stay on a private endpoint pins its own
under `[github.repos.openai]`; the rest keep the default.

Set `model` to a model the endpoint serves; `"auto"` takes the first one
the endpoint lists, which is the one a single-model box serves. Re-run
`sbxloop bake` after switching, so the template carries the worker's
`[openai]` extra.

What `sbxloop doctor` will and will not tell you: its credential row names
the configured variable and the endpoint it is bound to; its policy row
asks sbx whether the endpoint's host is allowed; and its endpoint row
reports whether the endpoint **answers from the host** — any HTTP answer
counts, a 404 for the model listing included. That last row says exactly
that and no more: whether the agent sandbox can reach a private endpoint
the host can is a separate question doctor cannot answer, so the row does
not imply it. If a run fails at its first model call with a connection
error while doctor is green, the sandbox's route to the endpoint is where
to look. Whether sbx's network policy accepts a bare hostname or an address
literal is **field-unverified**; provisioning stops naming the endpoint if
sbx refuses the allow or the binding, rather than leaving a sandbox to
fail later.

The backend runs the worker's own governed tools — the same read-only
reviewers, tool-call ceiling and host-tool relay as codex — against
`/v1/chat/completions`, with a session held as a transcript the worker
keeps. Native MCP servers are refused by name; credentialed HTTP MCP works
through host mediation as it does under codex. Token counts are reported
and no cost is invented: a served endpoint has no price. See the
[design and implementation plan](openai-backend.md) for the contract and
what stays **field-unverified** until it runs on a CI runner.

### Agent models

Choose a model for each of the eight run agents under `[agent.models]`.
`[concierge] model` selects the control-channel agent separately. For example,
with the Claude backend, use aliases supported by your credential:

```toml
model = "sonnet"

[agent]
backend = "claude"

[agent.models]
decompose = "sonnet"
build = "opus"
review = "sonnet"
steer = "haiku"
reauthor_verify = "sonnet"
operator_plan = "sonnet"
operator_execute = "opus"
operator_judge = "sonnet"

[concierge]
model = "haiku"

[[github.repos]]
repo = "your-org/your-project"

[github.repos.agent_models]
review = "opus"
```

Run-agent precedence is `--model MODEL`, then the selected repository's
`agent_models.<phase>`, then `agent.models.<phase>`, then top-level `model`.
Omit a key to inherit; setting `"auto"` explicitly asks the backend to choose
and stops inheritance. Unknown role names and blank role models are errors.
The concierge uses `concierge.model` or top-level `model`; repository choices
and a run's `--model` never affect it. Repo-less workloads use global settings.
Models do not change permissions, tools, credentials, or the backend.
Private dependency preparation uses the builder's model policy too.

These keys follow normal operator config layering: the home config, untracked
`pyproject.toml` (`[tool.sbxloop]`), untracked `sbxloop.toml`, then environment overrides such as
`SBXLOOP_AGENT__MODELS__BUILD`. Tracked project config cannot change model
policy. Put operator choices in the home config or an untracked file. The TUI
config editor opens a searchable model picker for top-level `model`, each
agent model, repository overrides, and `concierge.model`. Search by model name
or slug, use the arrow keys to select, and press Enter to apply. Choose
**Inherit** or press Ctrl+U to remove an override; **auto** explicitly delegates
selection to the backend. Ctrl+T opens custom entry for aliases or models the
catalog does not yet list. Search text alone never becomes a model value.

The picker reads the last successful catalog from the home's
`cache/models/<backend>.json`, showing its timestamp and whether it is stale.
Successful agent provisioning (including concierge reuse after restart)
refreshes missing or day-old catalogs in the background. Opening the picker
also refreshes a missing or stale catalog; Ctrl+R refreshes on demand.
`sbxloop list-models` refreshes the same cache after a successful nonempty
listing, including with `--json`. Failed discovery keeps the last successful
catalog and does not interrupt provisioning or discard configured model ids.
Catalogs are separate per backend and contain only model ids, display names,
policy state, and the fetch timestamp. The cache is advisory, not proof of
current account access; refresh it after changing credentials.

Discovery has the same host credential and optional SDK requirements as
[`list-models`](#which-models-can-i-use). Claude needs no host SDK extra;
Copilot and Codex require their respective extras. The picker reports lookup
failures and still offers the cached models, current value, auto, and custom entry.

Use `sbxloop doctor` for local model resolution and
`sbxloop list-models --repo your-org/your-project` to compare those choices
against the backend's catalogue. A model absent from that catalogue is
reported, not rejected: aliases and account-specific availability vary.

sbxloop rereads model settings automatically before each new phase, including
in active and resumed runs. The source directory and home stay pinned to the
run; environment overrides use the running process's environment (the current
environment after a restart). Other settings retain their existing lifecycle.
An invalid config or a backend change stops the next dispatch with an error.
A run's `--model` remains in force after edits and resumes.

An in-flight call, its JSON repair retry, and recovery of a provider-interrupted
call keep their original requested model. The next phase sees the new choice.
When a builder or operator changes model, sbxloop starts a fresh SDK session
with the existing workspace, task brief, feedback, and prior reports. It also
rotates the concierge session before its next turn if its model changes.
Legacy sessions without a recorded requested model start fresh conservatively.

`sbxloop status RUN` shows the next phase's model policy and its source;
terminal runs show their initial policy. Events separately record the requested
model and the SDK-reported model, so `auto` remains a request rather than a claim
about which model answered. Mixed-model usage reports show phase/model token
totals; unavailable usage stays unknown and no price is inferred.

## Quickstart

Everything sbxloop puts on a host lives under one directory, the **home**:
`~/.sbxloop` (`SBXLOOP_HOME` moves it). One command builds it — the
interpreter, the launchers, Docker's `sbx`, the config and secrets files,
and on Linux the systemd units. A home whose path holds spaces or a `%` is
fine; one holding a quote or a backslash is not, because systemd refuses
those in the executable of a unit, and `init --systemd` says so rather than
writing a unit that cannot start.

The host brings what that command cannot: curl, tar, git and e2fsprogs
(`mkfs.ext4`, for sandboxd's block driver). Git is a host dependency in its
own right — sbxloop reads and clones checkouts on the host, and the git a
sandbox carries is a separate one that does not stand in for it — so the
installer checks for a usable git, along with curl and tar, before it
downloads anything, and names what to install rather than failing later with
a traceback. `GIT_PYTHON_GIT_EXECUTABLE`, if you set it, is the executable
the check looks at, since it is the one sbxloop will use.

### Installation and host preparation are different jobs

Everything sbxloop installs lands under the home, which the invoking account
already owns — no step of the install needs root. What the *host* has to be
able to do is separate, one-time, and on Linux partly an administrator's:

| Capability                         | Who does it               | Without it                                                        |
| ---------------------------------- | ------------------------- | ----------------------------------------------------------------- |
| `/dev/kvm` exists                  | administrator             | no sandbox boots: a sandbox is a microVM                          |
| `/dev/kvm` openable by the account | administrator             | same, and the usual cause — Docker documents the `kvm` group      |
| `mkfs.ext4` (e2fsprogs)            | administrator             | Docker's sbx installer refuses to run, so `init` refuses too      |
| sbx's AppArmor profile in `/etc`   | administrator             | the sandbox backend cannot start; `init` says so and carries on   |
| a reachable `systemctl --user`     | log in as the account     | `init --systemd` refuses rather than writing units nothing runs   |
| `loginctl enable-linger <account>` | account, or administrator | the daemon stops at logout — unattended persistence is not set up |

None of this is done for you: no check joins a group, installs a package,
starts a sandbox, or elevates a privilege. Each is *reported* instead —
`sbxloop init` notes what it found, `sbxloop doctor` shows the same rows at
any time, and the access checks ask the kernel whether this account can open
the device rather than reading a group list (a group added in the current
shell does not reach a running process until the next login).

Only the relevant checks apply. On macOS, which brings its own
virtualisation, none of the Linux rows appear at all; a `--no-systemd`
install is never judged against a service manager it did not ask for; and a
`--no-sbx` install is never judged against `mkfs.ext4`.

```bash
curl -fsSL https://raw.githubusercontent.com/brettbergin/sbxloop/main/scripts/install.sh | sh
export PATH="$HOME/.sbxloop/bin:$PATH"
# (or, from an existing `pip install sbxloop`: `sbxloop init`)

# one-time host setup
$EDITOR ~/.sbxloop/config/secrets.env   # the tokens; sbxloop reads this file itself
sbx login
sbx policy init balanced
sbxloop doctor          # verifies the home, sbx, policy, tokens, worker wheel
sbxloop doctor --deep   # + full sbx conformance suite in a scratch sandbox

# go
sbxloop run "Add mypy strict typing to every module in ./src and fix all findings"

# while it runs / afterwards
sbxloop status                  # all runs; `sbxloop status <run>` for one run's tasks
sbxloop logs <run>              # the persisted event stream
sbxloop artifacts <run> --tree  # what the run produced
```

To update the home installed by `sbxloop init`:

```bash
sbxloop update --check    # show installed and latest versions from PyPI
sbxloop update --dry-run  # also show the installation command
sbxloop update           # install a newer release, if available
```

`update` uses the home's `uv` to install sbxloop and its worker at the same
version, keeps the chat extras and any installed host Copilot SDK, and
verifies both packages before recording the new version in `home.json`.
It leaves an equal or newer installed version alone. A failed lookup or
installation exits nonzero; a failed installation is not automatically
rolled back. Config, secrets, runs and services are left in place. Restart
any running daemon when idle to load the new version, then run
`sbxloop doctor` (and re-bake if it reports a stale template).

`--check` works from other installation types too. Automatic updates require
the running installation to belong to the selected `SBXLOOP_HOME`; checkouts,
pipx, `uv tool` and externally managed installations should use their own
installer, or `sbxloop init` to create a home. This explicit command checks
PyPI even when the daemon's background `version_check` is disabled.

`run` works on a checkout, and says which one before anything is
provisioned: `--workspace PATH`, else the checkout the config names
(`[sandbox] workspace` or a `[[github.repos]]` entry), else the git checkout
enclosing the directory you typed the command in. If the sandbox cannot see
that checkout the run stops there — it never quietly "succeeds" on an empty
directory. With no checkout anywhere the run says `workspace: none` and
works from an empty directory whose output is harvested as artifacts.

`run` opens a live chat-style dashboard by default: agent messages as
markdown panels, tool calls as compact lines, lifecycle events as dim
one-liners. `--no-tui` prints the same transcript sequentially (good for CI
logs); the full raw event stream is always available via `sbxloop logs`.

Optional, but cuts provisioning latency a lot: bake a sandbox template with
the worker preinstalled once, instead of installing it on every run.

```bash
sbxloop bake            # installs the worker + Copilot runtime into a template
# then set in sbxloop.toml:
#   [sandbox]
#   template = "sbxloop-baked:latest"
```

Runs verify the baked worker with fast probes and fall back to the normal
install if the template is stale (`sbxloop doctor` will tell you to re-bake
after upgrading sbxloop). The bake also installs the toolchains for the
configured `[sandbox] languages` (python by default) and records which ones
landed; a run whose languages the template lacks — a Go repo on a Python bake,
say — keeps the baked worker and provisions the missing toolchain on top, and
the `sandbox.prebaked` event and `sbxloop doctor` both say so, so you know
when a re-bake would stop paying for that per provision.

Baked templates also require `xz-utils`, even when JavaScript is not selected,
so later toolchain top-ups can extract `.tar.xz` archives. A bake fails if
that package cannot be installed; existing templates need a re-bake to gain it.
Provisioning retries confirmed apt/dpkg lock contention up to twelve times,
five seconds apart, within the original install timeout. Other apt errors
return immediately. If a toolchain's apt prerequisites fail, its installer
is skipped and the warning names the cause instead of attempting extraction
with missing tools.

The bake requires an isolated worker virtualenv. If the base image lacks
`ensurepip`, installation probes its running `python3` and installs the
matching `python3.X-venv` package (plus `python3-pip`) before retrying.
If repair fails, bake stops without saving a template; `--keep` retains the
scratch sandbox for inspection. Ordinary provisioning keeps the user-site
fallback for unusual images and logs the repair failure. Re-bake and set
`[sandbox] template` to the saved ref to avoid repeating installation on new
sandboxes.

#### Which models can I use?

Wondering what to put in `model = "..."` (or `--model`)? Ask the configured
backend which models your credential can actually use:

```bash
~/.sbxloop/bin/uv pip install --python ~/.sbxloop/venv/bin/python 'sbxloop[copilot]'   # copilot: the SDK is optional on the host
sbxloop list-models              # id, billing multiplier, context, reasoning, policy
sbxloop list-models --json       # machine-readable, for scripting
```

Under `[agent] backend = "claude"` the same command asks the Anthropic Models
API with `ANTHROPIC_API_KEY` (id, name, release date; no SDK needed on the
host).

Under `[agent] backend = "codex"`, install the optional host extra:

```bash
~/.sbxloop/bin/uv pip install --python ~/.sbxloop/venv/bin/python 'sbxloop[codex]'
```

`sbxloop list-models` then uses `OPENAI_API_KEY` and the Codex model
catalogue, including supported reasoning levels, without starting a model
turn. Running jobs needs no host SDK extra.

Under `[agent] backend = "openai"` the command asks the configured endpoint
itself — `GET <base_url>/models`, which vLLM, LiteLLM and the hosted API all
serve — with the key `api_key_env` names, over the stdlib (id and name; a
served listing carries no billing or context metadata). An endpoint that
answers 404 there serves no listing: the command says so and exits cleanly,
because the configured `model` is still valid to use and the model cache is
advisory. That cache is keyed by the endpoint as well as the backend, so a
listing from one endpoint is never offered for another.

Or as a library:

```python
from sbxloop import LoopEngine, load_config

engine = LoopEngine(config=load_config())
result = engine.start(outcome="Add mypy strict typing to ./src and fix all findings")
print(result.state, result.run_id)
```

### Platform support

sbxloop runs on Linux and macOS. The hard constraint is the sandbox layer:
every run, the daemon and the bake boot Docker Sandboxes microVMs through
`sbx`, which Docker ships for those two systems.

On **Windows** the supported path is **WSL2**: install a Linux distribution,
turn on Docker Desktop's WSL integration for it, and install sbxloop inside
that distribution — the install script, `sbxloop init`, the daemon and the
console all run there unchanged, and `sbxloop doctor` reports the host as
WSL. Developing sbxloop on Windows works the same way: clone and run the
test suite inside the distribution. Native Windows is refused by name:
`sbxloop run`, `resume`, `shell`, `daemon` and `bake` exit with a line
naming the WSL2 path before writing any state, and `sbxloop doctor`'s
first row (`host`) says the same. The read-only commands still answer so
the refusal can be diagnosed from the host itself.

What a **native Windows** host does support, precisely, is that diagnosis
— and nothing beyond it:

- **The home resolves from `USERPROFILE`.** A Windows session need not set
  a Unix `HOME`, so the home, `config\sbxloop.toml` and `config\secrets.env`
  are all found from `%USERPROFILE%\.sbxloop` (or `%HOMEDRIVE%%HOMEPATH%`).
  `SBXLOOP_HOME` overrides it as it does anywhere, spaces in the path and
  all. `doctor`, `config` and `logs` therefore read the same home the
  process runs out of, which is what makes the refusal legible.
- **`sbxloop init` writes a `bin\sbxloop.cmd`, not a shell script**, and
  points it at `venv\Scripts\sbxloop.exe`. It writes no `bin\sbx`
  wrapper: there is no native Windows `sbx` for one to stand in front of,
  and `init` says so in its notes.
- **Secrets are private by ACL, not by mode.** `chmod 600` does nothing on
  Windows — a file written that way still reports `0666` — so
  `config\secrets.env` is restricted with `icacls` and doctor's
  `secrets file` row reads the ACL back. A host whose ACL could not be
  read **fails** that row saying so, rather than passing a file it could
  not vouch for.
- **`sbxloop init --sbx` is not supported.** The sbx installer and its
  release assets are POSIX (`install.sh`, `.tar.gz`); installing the
  sandbox runtime natively is the WSL2 path above. The agent backends
  themselves are not the constraint here — the sandbox layer is.

## How a run works

```
outcome ──▶ DECOMPOSE (task DAG) ──▶ for each task, in dependency order:
              BUILD ─▶ VERIFY ─▶ done
                ▲        │fail (≤ revisions: same session resumes;
                └────────┘        exhausted: fresh session, one replan)

        ──▶ GATE ─▶ DELIVER (draft PR) ─▶ REVIEW ─▶ CI ─▶ LAND ─▶ merged
              ▲                            │changes requested / red / conflict
              └──────── FIX (one task) ◀───┘  (≤ max_review_rounds / max_ci_rounds)
```

- **Decompose** — produces the task DAG, and with it every task's
  `verify_commands` (the whole mechanical exam — the builder cannot edit
  them) and any declared network egress needs (see
  [Network egress](#network-egress-least-privilege-by-plan)).
- **Build** — one agent session plans and does the work in the sandbox
  workspace, narrating its approach first. It is told where it is — a
  feature branch of an existing repository that a human will review as a
  pull request, with the resolved toolchains and their versions named — and
  how to change code there: match the surrounding conventions, change only
  what the task requires (work beyond the outcome's scope is a defect, in
  the reviewer's own words), create no top-level files the task did not ask
  for. A revision resumes the same session; a replan (or a chat steer)
  starts a fresh one.
- **Verify** — mechanical: the task's `verify_commands` must exit 0, run from
  the workspace root. No LLM. The full command transcript is persisted with
  the attempt, so a resumed run judges with the real evidence. How much
  this decides is `[sandbox] verify_mode`: `full` (the default) gates;
  `advisory` runs the commands and reports; `ci-only` skips them and
  leaves the judging to the PR's checks — see "Suites that need services".
- **Gate** — the project's own check (`[sandbox] gate_command`, or the one
  the project declares — a `check`/`ci`/`verify` target in a makefile,
  justfile or Taskfile, a package.json script run under the client its
  lockfile names, tox, nox, a Rakefile task, a composer script, a Gradle
  wrapper, a `pom.xml`, a cargo alias — or, for Go, Rust and .NET, the tool
  itself) over the whole tree, mechanical. A detector only fires for a
  toolchain the sandbox was provisioned with — the task runners included:
  a Makefile, justfile or Taskfile selects `make`, `just` or `task` the
  way `go.mod` selects Go. The root is read first, then the same two
  levels of subdirectories language detection reads (test, fixture,
  example and docs directories excluded); a gate found below the root
  runs as `cd <dir> && <gate>`, a package.json script under the client
  the monorepo's root pins. A later task can break what an earlier one proved,
  so this is the last look at the tree exactly as it will be delivered. A run
  with no `[github] repo` ends **`completed`** here, its work in the
  workspace.
- **Deliver** — the tree becomes one commit on `sbxloop/<run>` and a draft
  pull request. Every later round re-delivers onto the same branch, so one
  run is one PR.
- **Review** — a fresh read-only session reads the PR's whole diff
  adversarially (concurrency, failure ordering, trust-boundary parsing,
  cross-module invariants, scope) and returns a verdict with line-anchored
  findings. The reviewer is told the project's gate as a result to weigh
  (or that the repository declares none), not as a step to run, and a diff
  the inline budget clipped says where the cut is rather than passing as
  unchanged. The verdict is the run's own and is authoritative; it is also
  posted to the PR for the record. There is no per-task critic: the old
  per-task review stages judged task completion and rubber-stamped it while
  diff-level defects leaked to the PR; one adversarial pass over the
  assembled diff is the critic that earns its turns.
- **Fix** — one seeded task (`fix-N`), built and verified like any other
  under the same revision/replan budgets, then back through the gate. Every
  round sees the earlier rounds' findings and the fixer's per-finding
  `addressed` / `refuted: <why>` list, and the next review may not re-raise
  a refuted finding without a rebuttal — the memory that stops a run arguing
  with itself. Every finding of the round is in the brief — blocking ones to
  address or refute, the rest to address, refute or `defer` to a follow-up —
  and a finding the fixer says nothing about is *unanswered*: it is carried
  into the next brief first, marked as such, and the reviewer keeps it at its
  severity, so a nit cannot be dropped on the floor round after round. Every
  blocking/major finding carries the reviewer's `repro`;
  the fix brief makes it a regression test that fails first, asks for the
  adjacent cases the same code path sees, and shows the fixer what earlier
  rounds decided — so rounds stop converging one case at a time.
- **CI** — the delivered head's check runs *and* commit statuses are
  polled (GitHub Actions and Checks-API apps alongside Jenkins, Buildkite,
  Travis, Codecov and anything else that reports through the Status API);
  red fetches the failing jobs' logs into the next fix brief, and a check
  whose log cannot be read from the sandbox (a commit status, or an
  Actions job the token cannot see) is briefed with its link and an
  instruction to reproduce with the project gate. For a red non-Actions
  check the worker also follows its `details_url` / `target_url`
  best-effort — unauthenticated, https only, text or JSON bodies, the
  same size clamp as an Actions log — and puts what it reads in the
  brief; the sandbox reaches only the hosts its policy allows, so a CI
  host worth reading goes in `[sandbox] extra_allow_domains`, and a
  failure to read leaves the brief at name, link and instruction.
  "Nothing has reported"
  on either API only counts as "no CI" once it has persisted for
  `ci_settle_s` — after a delivery, and again at landing for a head no
  poll has waited on (a resume at the landing stage, a merge-gate
  approve, the head an update-branch makes), so a slow CI's first run
  is never merged ahead of.
- **Land** — un-draft, update the branch if protection wants it current,
  merge with the head the review actually judged (a push that landed since
  loses the race rather than being merged over). Then, and only then, the
  review's out-of-scope notes (`followups`) and the fix rounds' deferred
  findings can be filed as **follow-up issues** on the repository — labelled
  `sbxloop:follow-up`, never the trigger label, so a human decides whether
  they run; deduplicated by title within the run and by a body marker
  against the repository, capped by `max_followups_per_run`, and listed in
  one PR comment. The reviewer must first look up related open and closed
  issues and compare the underlying problem. Existing matches are linked;
  declined issues stay declined. A regression needs a completed issue and
  fresh reproduction evidence. The host saves and rechecks each lookup:
  failed, incomplete, changed or missing evidence leaves a note on the PR
  for triage instead of creating an issue. This also applies to unchecked
  deferrals and old saved reviews resumed after upgrading. Lookup calls
  are bounded and use the existing reviewer session. Chat distinguishes
  newly filed issues, existing matches and notes needing triage.
  `[landing] followups = "comment"` lists them on the PR
  instead of filing; `"off"` drops them. A repository with Issues disabled
  cannot take them: filing downgrades to that one PR comment and the
  `run.followups` event records the downgrade (`downgraded_from`,
  `reason = issues_disabled`) — nothing is dropped silently. The issue
  body names the trigger label only when a daemon dispatched the run;
  under `sbxloop run` nothing polls the repository, so it says only that
  the follow-up is not queued. A failed or blocked run files nothing.

**Budgets, not vibes.** Revisions, replans, task count and wall clock are
bounded by `[budgets]` (defaults: 2 revisions and 1 replan per task, 20
tasks, 2 h wall clock, 30 min per job); the fix loop by `[landing]` —
`max_review_rounds` (default 3) for verdicts that request changes,
`max_ci_rounds` (default 2) for the mechanical failures: a red gate, red CI,
a base conflict, a human requesting changes on the PR. A run past either
budget is one round short, not broken — its branch is green and its PR is
open — so under the daemon the item's retry **resumes that same run** with
`retry_rounds` (default 2) more, once, instead of planning from scratch and
opening a second PR; a second exhaustion hands it to a human, and
`sbxloop daemon ctl grant-rounds <run> <n>` (or `!sbx grant-rounds`, or
asking the concierge for "two more rounds") resumes it at once with more.
Exhausting a task's budget fails the *task*; its dependents are skipped and the run finishes
`failed` before anything is delivered. One deliberate exception: when
revisions are exhausted by *verify-command* failures, the task spends a
replan first when budget remains — the builder cannot edit verify commands,
so only a fresh session's fresh approach can unstick work that disagrees
with where a check looks. Time spent waiting on GitHub is not charged to
`max_wall_clock_s`; `[landing] ci_timeout_s` bounds each wait instead.

**How a run ends.** `merged` — the PR landed; the work is on the base
branch. `completed` — no repository was configured; the work passed the
gate and sits in the workspace. `failed` — a task or a round budget ran out
(any PR is still a draft, and nothing re-picks it). `blocked` — the run
needs human triage. An empty delivery stops here without automatic retries:
check whether the request is already satisfied or changes were omitted or
excluded; an empty diff alone does not establish completion. GitHub can
also prevent a run that cleared its own bar from finishing: a protection rule
wanting an approval this identity cannot give, CI that never reported within
`ci_timeout_s`, an update-branch budget spent. Those landing blockers leave
the PR open and out of draft for a human. (`cancelled` is
the fifth, and yours.)

**Checkpointing and resume.** State is committed to SQLite after every
transition, and every stage is a run state — `building`, `gating`,
`delivering`, `reviewing`, `fixing`, `awaiting_ci`, `landing` — with the last
one entered kept on the run. `sbxloop resume <run>` re-provisions a fresh
sandbox pair and re-enters *there*: a crash during a CI wait costs a re-poll,
not a rebuild; a re-delivery is idempotent (the branch is force-moved, the
open PR reused); a `blocked` run resumes at `landing` once a human has dealt
with the cause. The run continues under its **persisted config**, not
whatever is on disk at resume time: the workspace is pinned from the state
DB (a mismatch refuses to resume), and any difference from the current
on-disk config is surfaced as a `run.config_drift` event. The one exception:
the debug toggles (`keep_sandboxes` / `keep_on_failure`) stay resume-time
choices, so a crashing run can be resumed with keep flipped on in config or
env.

**Guardrails.** The worker heartbeat samples in-VM disk and memory
(`[limits]`; defaults warn at 85 % disk / 90 % memory and abort the task at
95 % disk), so a runaway task fails with "sandbox disk exhausted" instead of
letting in-VM tooling fail confusingly on a full disk.

**Workloads.** `sbxloop run "…" --kind workload` starts the other kind of
run: an outcome that is not a change to a repository. It has the same shape
— a persisted run, a task graph, a resumable stage list — but boots the
agent sandbox alone (no repository, no GitHub box, no delivery), works in a
per-run data directory that is harvested as artifacts, and walks the
operator's stages instead of the developer's: `planning`, `executing`,
`judging` (every task's declared check re-run on the finished workspace; one
red check fails the run naming it), `publishing`. Two actors run inside it:
the **operator** plans the outcome into tasks — each with acceptance
criteria and the `needs` it declares by name (hosts, credentials from the
catalogue, a sink, a repository); a credential is never a value in its box
— and executes each task in the data directory, ending with a `## Result`
report; the **judge** then holds that task to its criteria in a read-only
session, reading the report as a claim against the data directory, the
record of the operator's tool calls and the task's declared checks, and
answers with a verdict (`judge.verdict`). A failing verdict quotes the
unmet criteria, which become the next attempt's brief under
`max_revisions_per_task`; past the budget the run fails naming the
criterion, and a judge that produces no usable verdict twice fails the
task closed (`judge.degraded`) — silence is never a pass. Every attempt
leaves the task an **output**: the `## Result` section of the operator's
report (its first line as a one-line summary) and the files the attempt
left in the data directory, persisted with the task and announced as
`task.output`. The run's result carries them — `sbxloop run` ends with a
closing line ("2/2 task(s) passed the judge" and one line per task),
`status <run>` lists each task's output, `status <run> --json` prints the
run, its tasks and their outputs as one object for scripts, and the
Discord finish card lists every task's result with the judge's word on it
where a code run's card shows the PR. `status` shows a run's kind; a code
run's trail is byte-identical with the flag absent.

**What a workload may ask for** is a named **profile** (#758). `[[workloads]]`
declares each one — `egress` (host patterns, as `[policy] allow`), the
`[[credentials]]` names it may be granted, the `sinks` a result may go to
(`chat`, `issue`, `artifact`, `pr`), whether a plan may ask for a `repo`
checkout, and `budgets` overrides for its runs — and `[workload] default`
names the one a run gets when `sbxloop run --profile NAME` does not choose
(the choice is pinned into the run's config, so a resume keeps it and sees
no drift). The planner is shown the profile's bounds, and right after the
plan, before any task runs, every declared need is held to them: a host
inside the profile's egress (and not denied) is allowed on the agent box
at the task's execute entry, a credential the profile names goes on the
run row and the run re-provisions from `executing` with the service sandbox
that holds it (one extra boot, only when a plan asked for a credential —
the agent box never holds the value), a permitted repository is checked
out into the data directory (`<data dir>/<name>`, read-only in spirit:
publishing to a repository is the `pr` sink's) — all announced as
`run.needs_granted`. A need outside the profile **fails the run closed**
before any task runs: every refusal is on the record as `run.needs_refused`
with the sbxloop.toml key that would allow it (`workloads.<name>.egress`,
`.credentials`, `.sinks`, `.repo`, `credentials` for a name not in the
catalogue, `github.repos` for an unconfigured repository, `workload.default`
when the run had no profile at all), the run's reason quotes the first,
and the Discord thread shows 🔐 / 🚫 lines. A run without a profile
declares no needs, or fails on the first. `sbxloop config show` lists the
credentials (set / unset, never a value) and the profiles as two tables;
`sbxloop doctor` adds a `workload profiles` row. `publish = "hold"` parks
a finished run at its publishing stage — judged, persisted, nothing
delivered — until a person releases it: the daemon posts a release prompt
in the run's thread (a **Release result** button on Discord), and
`!sbx release <item>` in chat, `sbxloop daemon ctl release <item>` headless
or `sbxloop resume <run>` for a CLI run all publish it; `!sbx abandon <item>` drops it unpublished. There is no deadline.

**Where the result goes** is the `publishing` stage's work (#759). Each task's
output goes to the **sink** its plan named in `needs.sink`: `chat` — the
default, needing no profile — is a reply where the run was asked for (the
Discord thread, or the terminal), carrying the run's closing line and every
chat task's result text; `issue` files **one** result issue per run in the
configured `[github] repo`, titled from the plan and carrying every task that
chose it, under the `[workload] result_label` (`sbxloop:result` by default,
created if missing); `artifact` copies the files a task reported — exactly
those, mounted or not — to `runs/<run>/artifacts`, where `sbxloop artifacts <run>` lists them
(the chat sink stages its tasks' files there too, and both sinks post them
into the thread as attachments up to `max_attachment_bytes`, naming larger
ones by host path) (a workload's whole data directory is salvaged to
`runs/<run>/data` instead, so the listing is the result and not the working
state around it); `pr` delivers the checkout a task asked for (`needs.repo`,
under a profile with `repo = true`) as **one** pull request — the working
tree's diff against the base, committed on the run's branch, titled from the
plan under the same result label — and that is the whole publish: no gate,
no review, no CI wait, and no run to settle (whoever owns the repository
merges). A profile that names `issue` or `pr` gives the run a github box;
`issue` is refused at grant time when there is no repository (`github.repo`)
or it has Issues disabled, `pr` when the task declared no `repo`. Sinks are
worked artifact → pr → issue → chat and each delivery is recorded on the run
(`run.published`, `status <run>` and `--json`, the finish card's
**Published** field) before the next, so a resume at `publishing` never
files a second issue or opens a second pull request; a sink that cannot take
the result — a checkout with nothing to deliver included — fails the run
naming it.

**How a workload is asked for** (#760) is the daemon's job, two ways in. A
GitHub issue carrying the **workload label** (`[daemon] workload_label`,
`sbxloop:workload` by default — the seventh lifecycle label, `init-repo`
creates it) is claimed exactly like a `sbxloop:run` issue — same claim
comment, same `in-progress` swap, same attempt and resume caps, same breaker
— but dispatched as a **workload** under the `[workload] default` profile,
with the issue's title, body and discussion as the ask. An issue carrying
*both* labels is refused before it is claimed: a comment says which to keep,
both come off, `sbxloop:failed` goes on. When the run finishes, the daemon
comments `Run <id> completed: <summary>` with one line per sink that
delivered, swaps `in-progress` for `sbxloop:completed` and closes the issue;
a task that chose the `issue` sink **answers on the asking issue** as a
comment rather than filing a result issue of its own. The other way in is
the concierge: `@sbxloop` a description of the work (naming a profile or a
sink if the default will not do) and it calls `start_workload`, queueing a
`chat:<message id>` item — no issue, no label, one tool call — that the same
poll picks up; the result comes back to the thread as usual, and an ask for
the `issue` sink files a result issue as before, since there is no asking
issue. A daemon with `[chat]` configured and no `[github]` at all is now a
valid daemon: chat-asked workloads are its whole queue. A profile's
`publish = "hold"` parks the finished run instead of publishing (see above):
the release re-queues the item with its run pinned and the next tick
resumes it at the publishing stage.

**Entrygraph reports from chat.** Ask the concierge:

- `Run entrygraph against the configured repos.`
- `Run entrygraph against owner/repo.`
- `Run entrygraph against https://github.com/owner/repo.git.`

or, from a terminal, `sbxloop run --kind tool --recipe entrygraph --target owner/repo "scan owner/repo"` — the same recipe path the daemon takes, with
the run's chronology in the TUI and the reports under
`runs/<run>/artifacts`.

The `start_entrygraph` tool queues one **tool run** per repository — a fixed
recipe with no agent in it: the pinned analyzer runs as one command in the
sandbox, its own consistency check runs behind it, and the two reports it
wrote go to chat as they are. No planning turn, no judge, no model-written
summary; the same input gives the same chronology every time. Discord
receives the report and the `report.md` / `report.json` attachments through
its existing run thread and upload limits. The report records the scanned commit, repository
statistics, languages/frameworks, entrypoints, and source-to-sink paths with
locations and confidence. Searches cover all source/sink categories, up to
100 paths and depth 25, and report widening, truncation and coverage limits;
an empty result does not establish that a repository is safe.

With no selector, scans cover all enabled configured repositories. A matching
configured repository URL uses that repository's existing credential; other
HTTPS clone URLs must be public. Credentials embedded in URLs are refused.
Configured checkouts are fetched through the existing host GitPython path;
public URL checkouts are cloned with GitPython inside the sandbox. Both are
isolated scan inputs, and target code and build setup are not executed.
Repositories with unavailable submodule or LFS contents retain that coverage
limitation in the report.

The recipe runs entrygraph 0.1.134 and installs its analysis runtime inside
the sandbox from published wheels, so the only egress it is granted is the
package index; `[policy] deny` still wins, and `[entrygraph] extra_hosts`
covers a sandbox where no wheel applies. `[entrygraph] enabled = false`
removes the tool; `allow_public_urls = false` narrows it to the configured
repositories. The usual queue limits, chronology, cancellation and resume
apply; steering does not — there is no agent to steer — and chat is the only
result sink. A tool run has no workload profile: its declared hosts are its
whole egress grant. Replaying the same chat message does not duplicate scans;
a new message queues a fresh scan.

**Workloads on a cadence** (#761) are the third way in: **schedules**,
which live in the daemon's database (#818), not in the config file. Create
one from chat — "every weekday morning, summarise what changed overnight":
the concierge interviews you (profile, cadence, timezone, with clickable
choices) and stores it through its `create_schedule` tool — or from the
host with `sbxloop daemon ctl schedules add <name> --profile P --every 1h|--cron 0 7 * * mon-fri [--tz ZONE] --ask TEXT…`.
Each carries a `name`, the `profile` to run under, the `ask`, and either
`every` (a period on a grid from creation) or `cron` (read in the timezone,
the daemon's `run_cap_timezone` when unset), and is live from the daemon's
next tick with no restart. Every tick queues the ask as a
`sched:<name>:<due>` workload item — the same run, grants and thread a chat
ask gets — and the run's terminal line in the control channel is its
record. A tick whose previous run is still live is skipped with a
`⏭ schedule … skipped` line rather than piled up; a tick is recorded at
its due time, so a late daemon does not shift the grid and one that was
down for several ticks catches up with one, not one per missed tick. A
daemon may run on schedules alone. `!sbx schedules` (or `sbxloop daemon ctl schedules`) lists them with the ask, who made them, the last fire and the next due; `schedules pause <name>` parks one — its ticks are skipped, not queued, until
`schedules resume <name>` — without pausing the daemon; `schedules remove <name>` (or the concierge's `delete_schedule`, on an explicit yes) deletes one. A `[[schedules]]` entry still in `sbxloop.toml` is imported into the database once on start and ignored after that; `sbxloop doctor` asks for it to be removed.

## CLI reference

| Command                                             | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sbxloop run "OUTCOME"`                             | Start a run; with a repository it carries the work through to the merge. Options: `--kind code\|workload`, `--profile` (a `[[workloads]]` profile, workload runs only), `--workspace`, `--repo`, `--deliver-base`, `--create-repo`, `--create-public`, `--model`, `--keep-sandboxes`, `--keep-on-failure`, `--no-tui`, `--no-chat`.                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `sbxloop daemon`                                    | The always-on outer loop: claim labeled issues, run each one through to a merged PR, settle the issue, mirror to chat (Discord or Slack). Options: `--repo`, `--max-runs-per-day`, `--poll-interval`, `--discord-channel`, `--slack-channel`, `--once`, `--dry-run`, `--log-level`, `--log-format`.                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `sbxloop daemon items\|abandon\|retry\|requeue`     | Inspect and steer individual work items from another shell without stopping the daemon (see below).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `sbxloop daemon ctl CMD`                            | Drive the running daemon from a script or cron: `status` (`--json` for one machine-readable object), `pause`, `resume`, `cancel`, `cancel-run <run>` (one specific run, wherever the daemon holds it), `resume-run <run>` (queue a persisted run for the daemon to resume), `queue`, `log` (the daemon's recent log records, `--tail`/`--level`/`--grep`), `stop` (finish the current run, claim nothing new, exit), `restart [--now]` (the same exit, then the service manager starts the daemon again and it says why when it is back; `--now` cancels the run in flight first; refused by name when nothing would restart it — see `[daemon] supervised`) — the same verbs as chat's `!sbx`, over a file queue in the home's `state/daemon/ctl/`. |
| `sbxloop daemon notify TEXT`                        | Post one message to the control channel through the configured `[chat] backend` — from the host, without the daemon, for deploy scripts and cron.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `sbxloop daemon logs`                               | Print the daemon's log file (`~/.sbxloop/logs/daemon.log`, the journal's twin): `--tail N`, `--follow`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `sbxloop tui`                                       | The operator console on the daemon host: overview, runs and their threads, the queue, sandboxes, daemon control and the journal, config (every setting editable one key at a time, comments kept), secrets, doctor — and the same chat experience Discord/Slack get, through the daemon's local bridge. `--run RUN` opens a run; `--read-only` observes only; `--state-dir` overrides the daemon's rule.                                                                                                                                                                                                                                                                                                                                             |
| `sbxloop resume RUN`                                | Re-provision sandboxes and continue a checkpointed run under its persisted config — at the task graph, or at the pipeline stage it stopped in (the retry path for a failed delivery or a `blocked` landing).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `sbxloop cancel RUN`                                | Cancel an in-flight run.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `sbxloop status [RUN]`                              | List runs, or show one run's task/phase detail.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `sbxloop logs RUN`                                  | The persisted event stream. `--type` filters by prefix (e.g. `--type policy.`), `--task` by task id.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `sbxloop artifacts RUN`                             | List a run's harvested files. `--tree` renders a tree; `--path` prints just the directory (for scripting).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `sbxloop shell RUN`                                 | Interactive shell in a run's sandbox. `--role agent\|github` picks the pair member; `-c CMD` runs one command.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `sbxloop init`                                      | Build (or repair) the home: tree, launchers, `uv` + CPython + the venv, Docker's `sbx`, `config/sbxloop.toml` and a 0600 `config/secrets.env` written once; `--systemd` renders and enables the units; `--migrate [--purge]` moves a pre-home installation in first; `--dry-run` prints the plan; `--project` writes a repository's own `sbxloop.toml` into the current directory (`--preset large-repo`, `--stdout`).                                                                                                                                                                                                                                                                                                                               |
| `sbxloop update`                                    | Check PyPI and install a newer sbxloop release into the running home's venv, with its worker pinned to the same version; `--check` only compares versions; `--dry-run` shows the installation command. Restart a running daemon when idle afterwards.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `sbxloop backup [list\|restore\|prune]`             | Snapshot the home's config, secrets, units and `state.db` into `backups/<stamp>/`; list, restore or prune the snapshots (the daily sweep keeps `[daemon] backups_keep`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `sbxloop init-repo OWNER/NAME`                      | Create the labels the loop relies on in a repository — the seven lifecycle labels (with that repository's renames applied) and the follow-up label, each colored and described. Idempotent; boots one github-ops sandbox; exits 1 when the token cannot write labels.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `sbxloop bake`                                      | Bake a sandbox template with the worker preinstalled (`--ref`, `--from`, `--keep`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `sbxloop doctor [--deep]`                           | Verify the host setup; `--deep` boots a scratch sandbox for the full sbx conformance suite.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `sbxloop sandbox ls\|rm\|prune`                     | Inspect, remove (`--run`, `--all`), or garbage-collect orphaned sbxloop sandboxes.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `sbxloop gc`                                        | Remove old run directories (workspace clones, harvested artifacts) past the retention window; `--older-than DAYS`, `--dry-run`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `sbxloop secrets list\|clean\|rotate`               | Manage the sbx custom-secret registrations sbxloop owns.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `sbxloop config show\|describe\|set\|unset\|policy` | Resolved configuration with per-key sources and when a change applies; one key's card (value, layer, what it accepts, what it is for); set or unset one key in the home's `config/sbxloop.toml` — comments kept, the whole file judged by the loader before it is written, the previous file kept as a backup; the effective egress policy.                                                                                                                                                                                                                                                                                                                                                                                                          |

## Network egress: least privilege, by plan

Sandboxes start with only the baseline allowlist: the Copilot/GitHub hosts,
the apt mirrors, and the supported languages' package registries (issue
[#141](https://github.com/brettbergin/sbxloop/issues/141) — no language's
build should fail for a reason another language's build never encounters).
Well-known registries outside that set are one notch narrower: not reachable
by default, but the decomposer may declare them per task in `egress` with no
configuration,
and every grant lands in the audit log. Anything else the decomposer
declares — each domain with a justification — is validated against
operator-set bounds:

```toml
# sbxloop.toml
[policy]
allow = ["nexus.corp.example.com"]  # what tasks MAY request
deny  = []                          # never grantable, even if allowed
```

Patterns are exact domains, `*.example.com` wildcards, or `*`. Empty `allow`
(the default) means tasks may only use the baseline and the well-known
registries. `deny` wins over everything, including the always-reachable
baseline: a denied registry is never seeded into the sandbox in the first
place. In-bounds grants are
applied **grant-late** — `sbx policy allow network` runs at EXECUTE entry, so
resumed runs re-grant on their fresh sandboxes — and every grant and refusal
is a `policy.allow` / `policy.deny` run event, making the persisted event log
an egress audit trail:

```bash
sbxloop logs <run> --type policy.   # who asked for what, and what was granted
sbxloop config policy               # the effective per-phase policy
```

Out-of-bounds requests fail graph validation with a remediation hint. Static
extras that every run should have go in `[sandbox] extra_allow_domains`.

### Chatting with a running loop

A run is not read-only: type a message into the TUI's input line (Enter to send)
and the agent pauses at the next checkpoint — the same phase boundary
cancellation uses — to answer it in a fresh **read-only STEER session** that can
inspect the workspace. The reply lands in the transcript, and the agent decides
what your message means for the work:

- **continue** — a question or status check; it answers and carries on.
- **steer task** — the current task's build session is discarded and restarted
  immediately with your guidance as feedback (user direction spends no
  revision/replan budget).
- **steer run** — your guidance becomes a standing instruction injected into
  every later build prompt, persisted so `sbxloop resume` keeps it.

Messages queue while a phase is in flight (the status panel shows them), every
chat turn is a persisted event (`sbxloop logs <run> --type chat.`), and
`--no-chat` disables the input entirely. With `--no-tui`, plain line input on
stdin does the same job.

## Working against an existing checkout

Point `[sandbox] workspace` at a project and runs execute on that code. When
the workspace is a **git checkout**, each run is isolated in a per-run clone
(`workspace_isolation = "auto"`, the default): the run works in
`~/.sbxloop/runs/<run>/workspace` on branch `sbxloop/<run>`, and your checkout —
its working tree, branches, HEAD — is never touched. Pull the results back
with the command the finish summary prints:

```bash
git fetch ~/.sbxloop/runs/<run>/workspace sbxloop/<run>
```

Dirty-tree rules: `auto` **refuses to start** when the checkout has
uncommitted changes (a clone takes committed HEAD, so they would silently not
travel — commit or stash first). `workspace_isolation = "clone"` isolates the
same way but proceeds from HEAD with a warning; `"in-place"` skips isolation
entirely and mutates the workspace directly. Clones hardlink git objects on
the same filesystem, so isolation is cheap; the working tree itself is
copied. If the agent commits inside the VM it needs `git config user.name` /
`user.email` — agents typically set these themselves.

Every run clone is cut `--single-branch --no-tags` (#632): it carries the
run's branch and its history, not every branch and tag the repository has
ever pushed. That is safe because the loop fetches the delivery base
explicitly before every merge-from-base and diff, so a base that is not the
clone's branch still resolves. Shallow clones are deliberately not used —
a `--depth 1` clone has no history to compute a merge base from, and a
wrong base is silently the wrong diff. For a very large repository without a
host checkout, `[sandbox] clone_filter = "blob:none"` opts the remote clone
into git's partial-clone filter: history and trees come down, file contents
are fetched lazily on first checkout. The hazard is that lazy fetch happens
wherever git next needs a blob, including inside the VM, which holds no git
credential (the run's token authenticates the host clone only) — fine for a
public repository, a mid-task failure on a private one, and the reason the
filter is opt-in and applies only to the remote clone. A git without
partial-clone support logs `workspace.clone_filter_unsupported` and clones
in full rather than failing.

**Submodules** are populated in every fresh run clone (#692), nested ones
included. A submodule comes from the host checkout's own copy of it when that
copy holds the commit the superproject records — no network, no credential —
and otherwise from its `.gitmodules` URL with the run's GitHub credential, the
same way the superproject's remote clone authenticates; the run's credential
must therefore be able to read the submodule's repository too. A submodule
neither route can populate fails provisioning naming it, rather than starting
the run on an empty directory where a dependency should be;
`[sandbox] clone_submodules = false` opts a repository whose submodules are
optional or unreadable out, leaving the directories empty. The hosts the
submodules fetch from join the agent sandbox's egress allow list, announced
as `sandbox.submodule_hosts` when they widen it, and
`sandbox.workspace_submodules` records what was populated from where. A
resumed run never re-populates: the submodule stays at whatever commit the
agent moved it to. At delivery, a submodule the run moved to a commit its
remote has is delivered as the moved pointer (a `160000` tree entry); changes
*inside* a submodule are never delivered — the pull request is against the
superproject — and are named in the PR body's **Not delivered** line instead,
as is a pointer at a commit the submodule's remote does not have.

**Git LFS** works the same way (#693). A run clone is cut with the pointer
files, and a fresh clone of a repository whose `.gitattributes` routes files
through `filter=lfs` is then populated from the host: every object the host
checkout's own LFS store holds is hard-linked into the clone — no network, no
credential — and whatever is still a pointer afterwards is fetched from the
repository's LFS endpoint (`<clone url>.git/info/lfs`) with the run's GitHub
credential, which must therefore be able to read the LFS store too. The host
needs `git-lfs` installed (`apt install git-lfs`; `sbxloop doctor` says so in
the `host git-lfs` row); without it, or when an object is missing and there
is no endpoint to fetch it from, provisioning fails naming the fix rather
than starting the run on pointer files. `[sandbox] clone_lfs = false` opts a
repository out and runs on the pointers. The clone's own config carries the
LFS filters, so a build that touches an asset's mtime does not turn it into a
change, and `sandbox.workspace_lfs` records how many objects came from where.
At delivery, an added or modified file that `.gitattributes` routes through
LFS is **not delivered** — the pull request API writes blobs, and committing
the asset's bytes where the repository expects a pointer would be worse than
refusing — and is named in the PR body's **Not delivered** line
(`deliver.lfs_change_skipped` in the log); deleting one delivers, since a
dropped pointer needs no object behind it.

**Tags** come back when the build needs them (#694). A `--no-tags` clone has
nothing for a build that derives its version from git tags — `setuptools_scm`,
`hatch-vcs`, `versioningit`, `poetry-dynamic-versioning`, `vergen`, Gradle's
`axion-release` / `nebula.release` / `git-version`, `MinVer`, `GitVersion`,
`Nerdbank.GitVersioning`, or a `git describe` in a Makefile — and such a
build fails, or quietly reports `0.0.0`. When a fresh clone's manifests
(`pyproject.toml`, `Cargo.toml`, `build.gradle`, `*.csproj`, `Makefile`,
`.goreleaser.yml`, …) name one of those, the loop fetches the repository's
tags into the clone: from the host checkout when it has tags (no network, no
credential), else with `git fetch --tags origin` under the run's GitHub
credential. A failed fetch fails provisioning by name rather than starting
a run whose version is wrong. `sandbox.workspace_tags` records what was
detected and how many tags came from where; `[sandbox] fetch_tags` is
`"auto"` by default, `"always"` for a build whose marker the loop does not
recognise, `"never"` to keep the clone tag-free.

### Environment for the agent sandbox

A project's test suite often reads its environment — `RAILS_ENV`,
`DATABASE_URL`, `GOFLAGS`. `[sandbox] env` (#679) puts that environment in
front of every command the agent sandbox's worker runs — the agent's own
turns and each task's verify commands alike:

```toml
[sandbox]
env = { RAILS_ENV = "test", DATABASE_URL = "postgres://localhost/app_test" }
```

`env` holds plain values, written into the config as given, and it has no
secret counterpart: the only credential the agent sandbox ever holds is its
own inference token, everything else lives in a sandbox the agent's commands
cannot read. A private registry's token goes on `[[registries]] auth_env`
(fetched from by the service sandbox; below), a service's key on
`[[credentials]]` (called through `call_service`; "Credentials a run may be
granted"), and a value a CI job needs goes to CI. An `env` value that names a
variable the loop delivers itself (`GH_TOKEN`, the agent credential,
anything `SBXLOOP_*`) or a registry's `auth_env` is refused at load. The
`[sandbox] secret_env` key of 1.0.x, which delivered a daemon secret into the
agent's sandbox, is gone (#766): a config that still carries it fails to
load by name — `sbxloop doctor` says so first — with the way forward in the
message. A `[[github.repos]]` entry can set `env` for its own runs; a set
value replaces the `[sandbox]` one.

### Private package registries

A repository whose `.npmrc` points at Artifactory, whose Python index is
private, or whose Go modules live on an internal host cannot install its
dependencies from the public baseline — so no gate can pass. `[[registries]]`
(#680, #766) declares each such registry once:

```toml
[[registries]]
kind = "npm"                 # npm | pypi | go | cargo | maven | nuget | gem | generic
host = "artifactory.example.com"
url = "https://artifactory.example.com/api/npm/npm-virtual/"
auth_env = "NPM_TOKEN"       # a daemon-environment variable — held by the service sandbox
scope = "@example"           # npm only: this scope; unset = the default registry

[[registries]]
kind = "go"
host = "github.example.com"  # → GOPRIVATE=github.example.com
```

An entry without `auth_env` is an **open** registry: its `host` joins the
agent sandbox's network allowlist (like `extra_allow_domains`, and in bounds
for a plan that names it) and the ecosystem's client configuration is
written into the agent sandbox before the worker installs, so the tooling
actually uses it.

An entry with `auth_env` is a **credentialed** registry. Its credential
stays in the run's **service sandbox**, which performs authenticated data
downloads from the configured HTTPS authority. Package managers and build
hooks run in the **agent sandbox**, which holds no registry credentials.
The host copies downloaded metadata, package archives or Git bundles into
the agent and verifies the transferred bytes. The service does not evaluate
manifests, extract packages or install dependencies.

Before your setup commands, one agent session resolves private dependencies
through `fetch_dependencies`, populates the ecosystem's offline cache, and
preserves dependency declarations and lockfile versions. The host then
verifies the ordinary dependency preparation command in the agent's offline
environment. Incomplete resolution or verification fails provisioning with
a resumable error. This preparation session uses the configured model and
per-job/tool-call budgets; its usage appears in the `dependencies` phase.

During a build or workload, the same tool can fetch dependencies after
manifest edits. Call it with `ecosystem` to discover registry names, URLs
and the cache location. Add `registry` and an absolute `path` to download a
file; optional `filename` and `sha256` name and verify it. `operation=git`
returns a Git bundle, with optional `ref` selecting a branch, tag or commit.
The response contains a local path in the agent, byte count and SHA-256.
The agent reads the files and uses its native tools to resolve transitive
dependencies and populate caches. A discovery query does not install anything.

The cache is `.sbxloop/deps/` in the workspace, excluded from Git and linked
at `~/.sbxloop/deps` in the agent only. Credentialed ecosystems use the
existing offline environment (`npm_config_offline`, `PIP_NO_INDEX` and
`PIP_FIND_LINKS`, `GOPROXY=off`, `CARGO_NET_OFFLINE`, `MAVEN_ARGS=-o`,
`NUGET_PACKAGES`, `BUNDLE_LOCAL`). Downloads are bounded to 1 GiB. Redirects
must stay on the configured HTTPS authority; credentials are never forwarded
to another host. Full resolution against private registries across all
ecosystems is **field-unverified**. No proxy or listener runs in either VM.

Open registries keep their existing agent-side configuration:

| kind      | agent configuration without `auth_env`                   |
| --------- | -------------------------------------------------------- |
| `npm`     | `~/.npmrc` with scoped or default registry URL           |
| `pypi`    | `PIP_INDEX_URL` and `UV_DEFAULT_INDEX`                   |
| `go`      | `GOPRIVATE`                                              |
| `cargo`   | `~/.cargo/config.toml` with the named sparse index       |
| `maven`   | `~/.m2/settings.xml` with the configured mirror          |
| `nuget`   | `~/.nuget/NuGet/NuGet.Config` with the configured source |
| `gem`     | `~/.gemrc` with the source URL, when set                 |
| `generic` | network allowlist only                                   |

`auth_env` is read from the daemon's environment at provision time; unset
names fail provisioning before a sandbox boots (the `registry credentials`
row of `sbxloop doctor` lists them), and the value never rides an `sbx`
argument, an event, or a log line. The service uses Bearer authentication
for npm, the registry token for Cargo, and HTTP Basic with `auth_user` for
the other kinds. No credential-bearing registry client file is written. A
derived variable a repository sets in `[sandbox] env` (its own `GOPRIVATE`)
wins over the registry's. A `[[github.repos]]` entry may carry its own
`registries` list, which replaces the top-level one.

### OS packages and setup commands

Toolchains carry the packages their own runtime needs; the library a
project links against (`libpq-dev`, `libjpeg-dev`), a compiler its build
shells out to (`protobuf-compiler`), a JDK beside a Python project, or a
one-off like `playwright install --with-deps` is the project's business —
and without a place to say so, the agent discovers it on its first failed
install and spends revision budget on `sudo apt-get`. `[sandbox] apt_packages` and `setup_commands` (#681) are that place:

```toml
[sandbox]
apt_packages = ["libpq-dev", "protobuf-compiler"]
setup_commands = [
  "npx playwright install --with-deps chromium",
  "pre-commit install-hooks",
]
```

`apt_packages` are ensured right after the toolchains, on the prebaked path
too: one `dpkg -s` pass names what the template lacks and the rest is one
`apt-get install`, so `sbxloop bake` with the list configured makes a run's
share a probe and no network. Unlike the toolchain ensure this is not
best-effort — the operator named the package because the project does not
build without it, so a failed install fails provisioning naming the package
and apt's last lines. `setup_commands` run in order, in the cloned workspace,
after the toolchains, the registries' client files and the sandbox
environment are in place and before the first agent phase; each runs under a
login shell with the same environment a job gets (per-job stdin delivery or
the in-VM env file) and under the
run's egress policy as already applied — a command that needs a host the
allowlist lacks fails here, not in a phase. Every command's exit code,
duration and output tail is a `sandbox.setup` event (delivered secret values
scrubbed). The tail is the command's own output: an image that announces
itself on login — a version manager, a banner, an MOTD — has that dropped, so
a banner cannot crowd the command out of the tail, while a launch that fails
before the command runs keeps everything it printed. The first non-zero exit
ends the run at provisioning with the command in the error, and `keep_on_failure` keeps the sandbox for `sbxloop shell`. A `[[github.repos]]` entry may carry its own `apt_packages` or
`setup_commands`, which replaces the top-level list; a per-repo package list
is paid at that repository's provision, since the bake reads the global list
only.

### Playwright MCP for browser verification

Give Copilot or Claude builders a headless browser with the packaged preset:

```bash
sbxloop init --preset playwright
# Inspect the generated config without writing files:
sbxloop init --stdout --preset playwright
```

For an existing installation, merge the generated `[sandbox]` settings and
`[[mcp]]` entries into the home's `config/sbxloop.toml`. Keep any existing
languages and append the setup commands to the existing list; a repository's
`setup_commands` override replaces that list and must include the browser
installation too. The preset selects `[agent] backend = "copilot"`; change
it to `"claude"` to use the Claude Agent SDK and configure that backend's
inference credential as usual. Native MCP is not supported by the Codex
backend.

The preset also gives builders [Context7](https://github.com/upstash/context7)
for current library documentation and code examples. It uses the hosted
HTTP server at `https://mcp.context7.com/mcp`, with no installation or API
key required; anonymous access has lower rate limits. Its `hosts` entry
allows the endpoint through the existing network policy.

The preset installs `@playwright/mcp@0.0.80` into
`$HOME/.sbxloop/playwright-mcp` inside the **agent sandbox**, then invokes
that package's Playwright CLI to install the matching Chromium binary and
Linux dependencies. This avoids changing the target's package manifest or
lockfile. The public package installation uses its own cache with lifecycle
scripts disabled, including when the target's dependency cache is offline.
The MCP command uses that installed copy, with `--browser chromium`,
`--headless` and `--isolated`. Each MCP browser session starts with a fresh
profile. See the [Playwright MCP options](https://github.com/microsoft/playwright-mcp#configuration)
and [browser installation guide](https://playwright.dev/docs/browsers#install-browsers).

Only the builder receives Playwright. It can start the target's development
server in the sandbox, navigate to its loopback address, inspect pages and
console errors, and exercise the UI. Ask it to save screenshots and other
evidence inside the workspace so they survive harvest. The declared hosts
cover npm and the browser downloads; add the application's external API,
asset and website domains to the MCP entry's `hosts` as needed. The existing
network policy and setup failure reporting still apply. Browser tools do
not replace the target repository's verification gate.

Setup commands run before the first phase of each **code run**; `sbxloop bake`
does not execute them. Workloads skip these commands, so the preset excludes
`operator`: to use it for workloads, first prepare a custom sandbox template
with the same package, browser and OS dependencies installed for the agent
user, select it under `[sandbox] template`, and add `"operator"` to `roles`.
Keep planners, critics and the concierge excluded. Update the pinned MCP
package and reinstall its browser together when upgrading.

### Suites that need services

Most backend suites want a database, a broker or a browser the sandbox
does not have, and a mandatory verify phase spends the task's revisions
and a replan on `connection refused` before giving up. `[sandbox] verify_mode` (#682) says how much the in-sandbox checks decide:

```toml
[sandbox]
verify_mode = "advisory"   # full (default) | advisory | ci-only
```

`full` is the gate as it was: a task's verify commands and the project
gate must pass. `advisory` runs them all and blocks on none — a failure is
a `phase.end` with `status = "advisory"` in the chronology (⚠ in the
channel), an evidence section in the review prompt, and a
**Verification** section in the pull request body, so the reviewer weighs
a `connection refused` against the diff instead of the loop spending
budget on it. `ci-only` submits no verify job and skips the gate stage
(both recorded `skipped`); landing's CI round, on a runner that has the
services, is the verification. A `[[github.repos]]` entry sets the mode per
repository. The mode never changes on its own: under `full`, a compose
file, `testcontainers` in a lockfile or a `services:` block in a workflow
is named once per run as a `verify.services_detected` event before the
plan is written — the moment a human can still turn the knob — and the
planner is told to scope verify commands to the subset that runs without
the services either way.

## Recovering from an agent provider limit

Claude throttling, exhausted quotas and insufficient credit pause the run
as `provider_held`. The chronology and `!sbx status` show the backend,
reason and next eligible time, or say that the reset is unknown. Other
work using the same backend credential waits too, including concierge
calls. Completed work, partial output, session context and usage are kept.

Transient failures allow three delayed retries (30, 60 and 120 seconds,
plus jitter), respecting any later provider timing. A quota with a known
future reset waits until then. Billing failures, unknown resets and
exhausted transient retries require operator recovery. The daemon
continues the same run; these waits spend no task-repair or crash-resume
budget and do not claim another full run.

After resolving the provider limit, use `!sbx resume <item|run>` to
continue its checkpoint, or `!sbx resume claude` to release the shared
Claude hold. `!sbx cancel <item|run>` stops a parked run and retains its
checkpoint; `!sbx pause` holds dispatch as usual. These commands do not
need an agent call and also work through `sbxloop daemon ctl`. For a
standalone run, `sbxloop resume <run>` is explicit recovery.

For an interrupted concierge call, repeat the original request after the
cooldown or explicit release. It continues the preserved session even if
the daemon's time or queue status changed while waiting.

If the interrupted SDK session is missing after partial work, or its
request has changed, recovery stays held for inspection. It never
silently restarts those side effects, changes models or credentials, or
changes billing arrangements.

## The daemon: an always-on outer loop

`sbxloop daemon` is deliberately small. It polls the one configured
repository (`--repo` / `[github] repo` — the repo being worked on) for open
issues carrying the trigger label (`sbxloop:run`), claims each one, runs it
as **one** engine run — task graph, gate, draft PR, review, fix rounds, CI,
merge — and settles the outcome on the issue. One labeled issue is one run
is one pull request. There is no other work source, and the daemon **never
files work of its own**: only a human labelling an issue — directly, or by
asking the chat concierge, which files the issue *with* the label —
starts a run.

The labels are the state machine, and every transition is visible on the
issue:

- **Claim.** `sbxloop:run` → `sbxloop:in-progress`, plus a claim comment
  (`<!-- sbxloop-claim <token> host=… pid=… started=… -->`) that doubles as
  the lock between daemons. The token is persisted *before* the comment is
  posted and SIGTERM is held until the claim is complete, so a process that
  dies mid-claim leaves a row the next start settles against the issue —
  finishing the claim if the comment landed, forgetting it if not. A claim
  comment from a dead process (this host, dead pid; or older than
  `[daemon] claim_stale_after_s` with no run started) is released and
  reclaimed, and a claim that turns out not to be ours (another daemon won,
  the issue closed, the label went away) leaves no row at all — the next
  poll re-creates it if the trigger label is still there.
  The comment is the lock: GitHub has no compare-and-swap on labels, but a
  comment is created exactly once and ordered, so two daemons watching one
  repository cannot both take an issue. A `Run <id> started.` comment
  follows once the run is dispatched.
- **`merged`** — the PR landed. The daemon comments the PR link, swaps
  `in-progress` for **`sbxloop:completed`** and closes the issue
  (`state_reason: completed`). The PR body also carries `Closes #N`, so
  GitHub links the pair and closes the issue even when the daemon is down.
- **`failed`** — the run gave up: a task or a round budget ran out, or it
  errored. The daemon comments the reason and retries with backoff while
  the item has attempts left (`max_attempts_per_item`, default 2;
  `retry_backoff_s` × the attempt number), then abandons it:
  `in-progress` → **`sbxloop:failed`**, with re-trigger instructions
  (just re-add `run`; the claim clears `failed` itself). Any PR stays a
  draft.
- **`blocked`** — the run needs human triage. No deliverable files or changes
  stops a code run without another automatic attempt, without a new PR, and
  without claiming the issue is completed. Check the current base and the
  intended changes before closing or re-triggering the issue. A run can also
  clear its own bar while GitHub would not let it
  finish: a protection rule wanting an approval the loop's identity cannot
  give, CI that never reported, an update-branch budget spent. The PR is
  left **open and out of draft** when blocked at landing; the issue stays
  open with **`sbxloop:blocked`** and a comment saying why, and the item
  neither retries nor counts toward the breaker, because nothing another
  attempt would change. Merge or fix by hand and close the issue, or re-add
  `sbxloop:run` once the cause is dealt with — that restarts it on the same
  branch and PR (`!sbx retry <item>` restarts from scratch instead).
- **cancelled** — `!sbx cancel` settles the item as cancelled, attributed
  to whoever asked, with no automatic retry; the run stays resumable.

**Restarting an issue: re-add the trigger label.** Re-applying
`sbxloop:run` to an issue whose last attempt finished — done, failed,
blocked or cancelled — re-queues it on the next poll whether or not the
issue text changed; the label is never silently inert. The restarted run
continues from whatever the previous attempt pushed to the GitHub origin:
the same branch and, if one was opened, the same (draft) PR, so its commits
are kept rather than redone. When nothing usable is on origin — no branch,
or a branch unrelated to the current base — the run simply starts fresh and
logs why. `!sbx retry <item>` remains the way to ask for a clean restart
from scratch.

Everything else the daemon does is a guardrail or a recovery. It is
**fully autonomous** — a label alone starts a run *and merges the result* —
so treat the trigger label as "execute arbitrary instructions with
`GH_TOKEN`'s repo scope" and restrict who can apply it. The `[daemon]`
guardrails are the safety net, and they are **daemon-wide**: they bound what
this host does in total, not what one repository does, so with several
repositories registered they are shared across all of them —

- a **calendar-day run cap** (`max_runs_per_day`, default 12) counting the
  runs *started* since 00:00 in `run_cap_timezone` (any IANA zone, default
  `UTC`) and resetting at that boundary, so a run started just before
  midnight does not free a slot early;
- the **per-item attempt cap** with backoff (above), and a **per-item
  resume cap** (`max_resumes_per_item`, default 2): an interrupted run
  (SIGTERM, crash) is resumed on the next start — through the same
  guardrails as any dispatch — and past the cap the interruption counts as
  a failed attempt instead;
- a **circuit breaker** (`max_consecutive_failures`, default 3, then
  `breaker_cooldown_s`, default 1 h) that is persisted, so a restart cannot
  reset it — and counts *consecutive failures across repositories*, so a
  repo that keeps failing pauses the whole daemon;
- **pause and cancel**, from Discord or `sbxloop daemon ctl` (below);
- **reconciliation**: on start, and every tick while nothing is executing,
  runs the store still shows in flight with no process behind them are
  closed with a recorded reason (`run_stale_after_s`, default 6 h; `0`
  disables the staleness sweep), so `sbxloop status` and `!sbx status`
  agree about what is active;
- **retention**: run directories past `prune_runs_after_days` are swept on
  start and daily (see [Sandbox hygiene](#sandbox-hygiene)).

Polling and issue lifecycle run through a long-lived github-ops sandbox the
daemon owns, so the host still never holds the PAT. Runs are one at a time,
across every configured repository. Ship it as a systemd user service with
[`contrib/systemd/`](../contrib/systemd/).

Individual items are steerable from another shell without stopping the
daemon: `sbxloop daemon items` lists them (state, attempts, pinned run, last
error); `sbxloop daemon abandon <item> [--reason …]` gives one up (a live
daemon cancels its in-flight run and tells the issue — the report is owed
on the row and paid by the next tick or the next daemon start, once);
`sbxloop daemon retry <item>` re-queues an abandoned, blocked or cancelled
item with attempts reset and a **fresh build session** — not a resume of
the approach that failed; and `sbxloop daemon requeue <item>` drops a
running item's pinned run so its next dispatch starts over (attempts and
backoff kept). The same controls are `!sbx items|abandon|retry|requeue` on
Discord.

`<item>` is a work item id. Forge items are **typed** —
`gh:issue:<number>` for the issue a run was claimed from, `gh:pr:<number>`
for a pull request referenced as a work-item resource — and the untyped
legacy form `gh:<number>` is still accepted everywhere as an alias for
`gh:issue:<number>`, so old commands, checkpoints and watches keep working.
The prefix names the forge: `gh:` is GitHub, and the same grammar reads
under `gl:` (GitLab) and `gt:` (Gitea) for the backends to come.
Everything sbxloop prints uses the typed form. See
[Work item ids](architecture.md#work-item-ids) for the full grammar.

**Workspace posture for unattended runs.** The daemon keeps a **dedicated
clone nobody edits** of each repository under
`~/.sbxloop/workspaces/<owner>/<name>`, cloned on first use; point
`[sandbox] workspace` (or a repo entry's `workspace`) at a checkout of your
own only to use that one instead — never the checkout you work in. Before
each fresh run the daemon `git fetch`es that clone and fast-forwards its
branch to its upstream (the
remote the branch tracks; `origin/<branch>` when none is configured) — never
a merge or rebase; a diverged branch or a colliding local edit is left alone
and logged — so runs start from the current remote branch rather than a
stale local HEAD (`[daemon] refresh_workspace`). Daemon runs use `clone`
isolation regardless of `[sandbox] workspace_isolation` (`[daemon] workspace_isolation`, default `clone`): a dirty tree proceeds from committed
HEAD with a warning, because `auto`'s refusal has no human present to answer
it. Per-run clones point their `origin` at the source's origin URL (metadata
only; any userinfo such as an embedded token is stripped from the URL, so
no credentials leave the host). And the daemon keeps its state under the
home (`~/.sbxloop/state`, runs under `~/.sbxloop/runs`), never inside a
checkout, so a checkout never accretes one full clone per run. The daemon
logs its home at start (in its `daemon.starting` summary), and every other
command — `status`, `logs`, `gc`, `daemon items|abandon|retry|requeue`,
`tui` — reads the same home from any directory.

The daemon's log stream (stderr → journald under systemd, and the same
records in `~/.sbxloop/logs/daemon.log`, rotated by size) is structured:
`--log-level DEBUG|INFO|WARNING|ERROR` (`[daemon] log_level`,
`SBXLOOP_DAEMON__LOG_LEVEL`; default `INFO`) and `--log-format console|json`
(`[daemon] log_format`; `json` is one object per line for log shippers). At
`INFO` you get the startup config summary, every claim, `run.dispatch` /
`run.finished` with durations, the run's own lifecycle mirrored under the
`sbxloop.run` logger (task/phase transitions, sandbox provisioning, worker
jobs, steering), operator commands, and why the daemon is idle (paused,
breaker open, backing off, capped) whenever that changes; `DEBUG` adds every
tool call, `sbx` invocation and poll. See
[docs/architecture.md → Logging](architecture.md#logging).

#### The `[daemon]` keys

This is the whole list — the landing knobs live under
[`[landing]`](#github-integration):

```toml
[daemon]
poll_interval_s = 60.0
trigger_label = "sbxloop:run"             # the label that queues work
in_progress_label = "sbxloop:in-progress"
completed_label = "sbxloop:completed"     # the PR merged; the issue closes
failed_label = "sbxloop:failed"           # the run gave up; re-trigger by hand
blocked_label = "sbxloop:blocked"         # GitHub would not let the loop land the PR
gated_label = "sbxloop:awaiting-merge"    # parked by [landing] merge_gate
workload_label = "sbxloop:workload"       # queues an issue as a workload, not a code run
max_runs_per_day = 12                     # calendar-day cap, persisted across restarts
run_cap_timezone = "UTC"                  # day boundary for the cap (resets at 00:00 there)
max_attempts_per_item = 2
max_resumes_per_item = 2                  # interrupted runs resumed at most this often per item
retry_backoff_s = 900.0                   # times the attempt number
max_consecutive_failures = 3              # circuit breaker ...
breaker_cooldown_s = 3600.0               # ... and how long it stays open
shutdown_grace_s = 60.0                   # keep below systemd TimeoutStopSec
prune_runs_after_days = 14                # run-directory retention; 0 disables
backups_keep = 10                         # snapshots kept under ~/.sbxloop/backups; 0 keeps all
run_stale_after_s = 21600                 # staleness reconciliation; 0 disables
workspace_isolation = "clone"             # clone | auto | in-place, for daemon runs
refresh_workspace = true
log_level = "INFO"
log_format = "console"
version_check = true                      # ask PyPI once at start; false = no request, no advice
# upgrade_command = "pipx upgrade sbxloop"  # what the drift notice tells the operator to run
```

#### Upgrading a pre-1.0 daemon

The 1.0 pipeline retired the daemon's other lanes — the agent backlog,
post-mortems, scheduled audit charters, the review lane, the inbox source,
the per-run tracking issue — and with them their `[daemon]` keys and
`[github] report` / `deliver`; the landing knobs (`deliver_draft`,
`merge_method`, `delete_branch_on_merge`, `merge_update_attempts`) moved to
`[landing]`. A config still carrying a retired key **fails to load** (every
config model forbids unknown keys), so delete them before upgrading a
0.7.x host straight to 1.0 — the 0.7.55–0.7.56 releases loaded them with a
warning and a `sbxloop doctor` row to make that edit unhurried
(`auto_merge = true` simply goes: landing is always on). A pre-1.0
`state.db` is moved aside to `state.db.pre-1.0` on first start rather than
migrated, and the old lanes' issues and labels are closed by hand;
[CHANGELOG → 1.0 cutover](../CHANGELOG.md#10-cutover) has the steps.

### Chat: chronology out, steering in — Discord, Slack or Mattermost

The daemon's human channel is one chat service, chosen by `[chat] backend = "discord" | "slack" | "mattermost"` — or inferred from whichever of
`[discord]` / `[slack]` / `[mattermost]` carries a `channel_id`; configuring
more than one without choosing is a config error, and none means the daemon
runs headless (`sbxloop daemon ctl` only).
Everything in this section works the same on each: Discord is described
first, the Slack and Mattermost differences follow. With `pip install 'sbxloop[discord]'`,
`DISCORD_BOT_TOKEN` in the environment, and `[discord] channel_id` set, a
gateway bot posts a headline card per run in the control channel (source issue, run id, branch, PR,
task tally — colour follows the state) and streams that run's
chronology into a thread under it, in Discord's own formatting: agent
messages as Markdown with persona attribution, split at paragraph and
code-fence boundaries instead of clipped — their **narration only**, never the
JSON payload a structured phase returns; what that payload *decided* is posted
in its own words instead: the task roster (`🧩 3 task(s)`, re-announced with
persisted state on resume) — while the builder narrates its approach in
prose, and each attempt closes with a `🔨 build` report-excerpt line; each burst of tool calls
digested into **one line edited in place** (`⚙ 23 tool calls (bash x21, view x2) — last: pytest -q`, with a "may be stuck" nudge when the last
calls are near-identical) — failed calls still get their own detail
block, and `chronology_level = "verbose"` streams every call batched into
code blocks instead; one **status line edited in place** as tasks
progress (`⏳ task 2/5 · Add tests · verify`); issue, PR
and branch as links; verify failures, worker errors, denied permissions and
refused egress called out; and a finished report card (the headline turns
✅/❌/⚠) with the final state, the task tally and the PR. How each item
settled is a one-line notice in the control channel, pointing at the run's
thread — `🎉 gh:issue:9 merged (2/2 tasks done) · PR …`,
`❌ gh:issue:4 failed (…); 1 attempt(s) left`, `🚧 gh:issue:7 blocked: … — a human needs to look` when an issue lands in `sbxloop:blocked`, `🛑 circuit breaker opened …` — with every URL masked so nothing sprouts a preview.
With `[landing] merge_gate = "chat"` — the one opt-in human touchpoint — a
run that clears every bar parks instead of merging: `⏸ ready to merge — waiting for your approval` lands in the run's thread @mentioning whoever
asked for the work — with a persistent **Approve merge** button on
Discord — and `!sbx merge <item>` (here or in the control
channel; `sbxloop daemon ctl merge <item>` works headless) completes the
landing, while `!sbx abandon <item>` declines and leaves the PR open. No
deadline; the park, its prompt and its button survive restarts.
A base branch that *requires* an approving review is not a block: the run
parks `awaiting_review` — PR un-drafted, `[github] reviewers` requested,
`👀 awaiting review` in the run's thread @mentioning the requester and
`[landing] review_notify` — and the daemon polls the PR every
`review_poll_interval_s`. A human's approval (or a human merging it) lands
the run; a changes-requested review resumes it for a fix round; the PR
closed abandons it. After `review_wait_s` without a verdict the item goes
`paused_review` (one more mention, no more polling) until
`!sbx resume <item>` picks it up again. The park survives restarts.
A person converting the PR to draft is the same park with a different
end: `✋ held in draft` in the thread, no reviewers requested, and the poll
waits for the PR to be marked ready for review — approvals alone do not
end it — then completes the landing without ever un-drafting on its own
(`review.ready`). A landing that comes back waiting on something else
(the base grew a review rule; someone re-drafted it) re-parks the hold for
that (`review.reparked`) rather than failing it.
Mentions are otherwise always disabled, so model output can never ping the
channel, and Discord's automatic link previews (unfurls) are suppressed on
every send *and* every edit, so no message sprouts a grey preview card — the
bridge's own embed cards still render. `[discord] embeds` (set `false` to
render those cards as plain-markdown twins instead; unfurl suppression is
unaffected), `status_line`, `tool_batch_lines`,
`tool_output_lines` (tail output lines echoed for a *successful* call,
default `0` = none) and `tool_fail_output_lines` (head+tail lines echoed for a
*failed* call, default `20` — a watcher needs the stderr) tune it, along with
`chronology_level`. Excerpts are line-clipped, body-capped and clamped to
Discord's 2000-character message limit, with any elision marked
`… N lines elided …`. **@mention the bot in a run's thread to steer that run**
(or reply to one of its messages there) — the same rule the control channel
uses, so people can talk about a run in its own thread without derailing it.
Your message is
relayed to the agent exactly like the CLI's `--chat` (answered at the next
checkpoint, which can be minutes into a long step — a note under your
message says where the agent is, `⏳ steer queued — agent is mid-execute on t2 (12/40 tool calls so far)`, edited in place until the ⏳ reaction turns ✅
when the reply lands).

Anything you address to the bot — a steer, or an @mention in the control
channel — is marked on your own message as it moves: **⏳ received**, then
**✅ answered** or **⚠ something went wrong**. The ⏳ goes on the moment the
message is routed, before the work behind it starts, so it is there while
the concierge is still thinking rather than arriving with the reply; a
steer the run can no longer take settles to ⚠ rather than leaving a clock
for an answer that will never come; and a message that gets a second turn
against it later (answering a clarifying question does) is never marked
received again after it has been answered.

`!sbx status|pause [--hold NAME]|resume [--hold NAME|--all]|cancel [--retry]|queue|items|abandon <item> [reason]|retry <item>|requeue <item>|merge <item|run>|release <item|run>|grant-rounds <run> <n>|resume-repo <owner/name>|schedules [pause <name>|resume <name>]|log [--tail N] [--level L] [--grep T]|stop|restart [--now]` in the control channel drive the daemon
itself. Pause is a set of **named holds**: a bare `pause`/`resume` acts on the
operator's hold, the deploy pipeline holds `deploy-<run id>` while it waits for
the daemon to go idle, and the daemon idles while any hold stands — so an
operator pause survives a deploy and `resume --all` is the override for a hold
whose owner never released it. `!sbx cancel` stops the current run at its next boundary and settles
the item as **cancelled** — attributed to you on the source, no automatic
retry, no breaker count — while the run stays resumable (`sbxloop resume RUN`
on the daemon host); `!sbx cancel --retry` re-queues it for a fresh run
instead, and `!sbx retry <item>` reruns any cancelled or abandoned item with
its attempt budget reset. Every `<item>` argument takes either the typed
`gh:issue:<n>` / `gh:pr:<n>` form or the legacy bare `gh:<n>`
([Work item ids](architecture.md#work-item-ids)); replies always quote
the typed form. Those verbs work in a run's thread too, answered
where you typed them. Anyone who can post in the channel
can steer — that is the boundary to set. The bot ignores messages from bots
(itself included), so scripts drive the daemon with `sbxloop daemon ctl <verb>`
instead — the same verbs through the same dispatcher, no Discord needed; a
request no daemon picks up within `--timeout` (30s) is withdrawn, so a stale
`cancel` never fires when the daemon starts later. Timing out is not "not
executed": once the daemon has taken a request it keeps running (item verbs
cross the ops sandbox), and `ctl` reports it as pending (exit 1) rather than
absent (exit 2). Scripts that need the state rather than the prose read
`sbxloop daemon ctl status --json` — one JSON object with `current`, `claiming`,
`holds`, `paused`, `source_failures` and `source_retry_in_s` — and post their own notices with
`sbxloop daemon notify "<text>"`, which goes through the configured chat backend
from the host even while the daemon is down ([docs/deploy.md](deploy.md)).

Polling failures are separate from failed runs: an idle daemon with zero run failures can
still be unable to discover work. Status warns when polling is failing and shows when it
will retry. After a Docker authentication outage, the next provisioning attempt retries
cleanup of the daemon's stale GitHub sandbox; restoring login does not require another
daemon restart.

**Chat with the daemon.** @mention the bot in the control channel (or reply
to one of its messages) and the **concierge** answers — the channel's own
agent, which knows how to operate sbxloop and what it is building. Ask
"what's running?", "why did `r7…` fail?", "show me the diff of that PR",
"pause after this one", or "also please add retries to the fetch client"
— it runs the same `!sbx` verbs through the same dispatcher, reads the
run store (runs, tasks, chronology, reports), fetches PR/issue/diff/file
details through the github-ops sandbox, and turns a described feature or
bug into work in **one hop**: `create_issue` files the issue in the
configured repo with a self-contained title and body *and* the
`sbxloop:run` label, and the daemon claims it on its next poll (backlog
capture, triage notes and canaries take the explicit opt-in path instead —
filed with no trigger label, left for `label_issue_for_run`). What the run
then reads is the whole issue, not just its title and body: the comments
under it (minus the loop's own claim and status comments, and its identity's
where it can resolve one) and the issues and pull requests they link to — on
a real tracker the body is a one-liner and the repro, the maintainer's
scoping and "do it the way #123 did" live in the thread. The discussion is
capped at `[budgets] outcome_max_chars` (16,000 characters; the body is
never cut, and the cut is marked), and a thread that could not be read is
said so in the outcome rather than silently missing. There is
no triage lane in between — which is why the concierge writes a body a
fresh clone can act on, and why the channel is the access boundary. That
body is **symptom-first**: what the person observes today in their own
words, the change they asked for as a hint, the concierge's restatement of
the goal, and acceptance criteria written against the symptom — because the
loop optimises hard for the words in the issue, and an issue that names a
mechanism gets exactly that mechanism (#519 asked for "the embeds" removed
and meant Discord's link unfurls). A fix-shaped ask with no observed
symptom is the one thing the concierge asks about before filing: "what are
you seeing that you want gone?" — one question, then the issue. The
decomposer plans against the symptom and the reviewer judges the PR against
it, so a change that implements the mechanism without curing the symptom is
sent back in round 1.

**Clarifying questions you answer by clicking.** When the concierge needs
one more thing from you *and* the plausible answers are enumerable — "is
this about the wording, the layout, or the timing?", "close #12 as a
duplicate, or as completed?" — it posts the question with a **button per
answer**, so unblocking the bot is one click rather than a typed reply.
Clicking is the whole answer: the daemon feeds the selected option back
into the conversation exactly as if you had typed it, so the outcome is
identical either way, and the message is edited to record which option was
chosen and by whom.

Typing still works, always. The buttons are an extra way in, never the only
one: the same numbered options stay in the message body, so "2", "the
layout", or an answer in your own words is understood just as it was before
— and an answer that names none of the options is passed through to the
concierge as ordinary prose, unchanged.

Not every question gets buttons. When the answers are **not** enumerable —
"paste the traceback you saw", "what should the new title be?" — the
concierge asks free text and the message carries no components, rather than
forcing you into an unsuitable set of choices.

The interactive message degrades safely. An outstanding question stays
clickable for 15 minutes; after that the buttons are greyed out with a note
that typing still works, and a click that arrives late (or on a question
already answered, or after a daemon restart, which forgets them — nothing
is persisted) gets a private nudge to answer in the channel instead. The
bot never waits on a click: a Discord that rejects the components, or a
host without them, simply gets the plain numbered question. Every backend
posts that same numbered prose and stays answerable by typing; what differs
is the affordance laid on top. Slack adds Block Kit buttons. Mattermost
seeds the question with one emoji per choice — reacting 1️⃣/2️⃣ answers it —
because Mattermost's own interactive buttons post to a callback URL, which
would cost the daemon the dial-out property its bridge is built on, while a
reaction arrives on the websocket already open.

Answering settles the message the same way everywhere: the question stays
readable, the chosen option and who chose it are recorded under it, and the
affordance goes — Discord's buttons disappear, Slack's blocks drop, and
Mattermost's seeded digits are taken back off, so nobody reacts to a
question that is already answered. A reaction that arrives on a Mattermost
question the daemon no longer holds is answered in the thread under it
(a bot account there has no private note to send), once, however many
people try it — for the life of the daemon process that asked; one that
restarted in between no longer recognises the post and stays quiet.
"What's open?" lists the repository's open issues and which are queued or
running; `queued: false` shows everything the daemon is not currently
queued or running — the backlog plus issues that failed or are blocked and
need a person — and a `state` argument narrows to one exact state.
Ask what a run cost and it reports that run's input/output tokens per
agent persona and totalled; "how much have we spent today?" totals the
current calendar day in `run_cap_timezone` — the same day the run cap
counts — next to that cap. Tokens are attributed to when they were spent,
so a run spanning midnight counts on both days. The
backend reports tokens but not cost, so it says that rather than
converting to money — and a run from before usage reporting answers "not
recorded", never zero.
Ask "are we up to date?" and it compares the installed `sbxloop` /
`sbxloop-worker` / `sbx` versions against the latest releases on PyPI —
sbxloop's releases ship frequently while upgrading a host is an operator's
step, so the daemon also says so once at startup when it is behind. (It
only reports: the advice names `[daemon] upgrade_command` when one is set
and otherwise says the command depends on how sbxloop was installed; a
restart follows either way. `[daemon] version_check = false` switches the
PyPI lookup off entirely — no request leaves the host, no notice is posted
— for an air-gapped or mirror-pinned host, or one a deploy pipeline keeps
current.)
Ask "what is the daemon doing?" or "why is nothing running?" and it quotes
the daemon's own recent log lines — `daemon.idle`, `breaker`,
`github.poll_failed` — through `daemon_log(tail, level, grep)`, the journal
without ssh. It reads a **bounded in-process ring buffer** the running
daemon fills (the last 2000 rendered lines, already redacted), not the full
systemd journal: anything older than the buffer, or from a previous daemon
process, still needs `journalctl --user -u sbxloop-daemon`. `tail` is how
many records (default 50, at most 500), `level` keeps only records at or
above `DEBUG`/`INFO`/`WARNING`/`ERROR`, and `grep` is a plain
case-insensitive substring — never a regular expression, so no pattern from
chat can wedge the daemon. The result is clipped to
`[concierge] max_tool_result_chars` like every other tool result.
Say "tell me when r7… is done" (a run id or a work item id) and `watch_run`
registers your interest: it confirms, and when that run lands the daemon
posts in the control channel @mentioning you with the outcome — final
state, task summary, PR, and the reason when it failed or was blocked.
Watching a run that has already finished answers with the outcome
immediately instead of registering. Watches are **persisted** in the daemon state: they are
reloaded at startup, so a watch registered before a daemon restart still
pings you when the run lands.
Ask "what is the daily run cap?", "what values does `merge_method` take?"
or "what can you change?" and `config_keys` answers from the operator's
`config/sbxloop.toml` as it is on disk, every other layer applied — never
from memory: the sections first (how many keys, how many the file sets),
then one card per key with its value, the layer that set it, what it
accepts, whether a change applies live or at the daemon's next start, and
what it is for. A key of one repository is addressed by `owner/name`. The
chat sections and the concierge's own switches are marked *never from
chat*. "Raise the daily run cap to 20" is `set_config`: the concierge shows
the key's card, the value as it will be written and whether a restart is
needed, and offers **Set and restart now** / **Set and restart after the
current run** / **Set only** / **Cancel**; on your yes it writes the one
key into the home's `config/sbxloop.toml` (comments kept, the whole file
judged by the loader before it is written, the previous file kept as a
timestamped backup) and, unless the key applies live, restarts the daemon
— which, when it is back, posts whether the change is in effect or another
layer still wins. It is the second thing the concierge never does on its
own initiative, after closing an issue, and it never proceeds on silence.
`[concierge] edit_config` gates both tools; `[concierge] config_locked`
lists the prefixes chat may read but not change (egress, tool grants and
credential names by default), and the chat sections and the gate itself
are never changed from chat whatever it says.

It finishes triage too: "reply on #12 that we're waiting on upstream"
posts a comment signed with your name, and "close #12 as a duplicate of
#7" comments and closes it as *not planned* (or *completed*) — but only
after it has asked and you have said yes naming the issue, and never while
a run is working that issue. `[concierge] create_issues` gates all of it.
Actions are otherwise direct — it acts
with the same authority as `!sbx`, so anyone who can mention it drives the
daemon; restrict the channel accordingly — and every tool it used is
listed in one edited `🛠 concierge: sbx_control(status) · run_detail(r7…)`
line under your question, so nothing happens invisibly. Steering a live
run still happens by @mentioning the bot in that run's thread; asked from
the control channel, the concierge points at the thread. It runs as a Copilot session in a
**long-lived agent sandbox** the daemon owns (`sbxloop-concierge-<digest>`,
reused across daemon restarts so the conversation keeps its memory; the
SDK session is rotated after `[concierge] session_turns` messages) and
reaches the daemon only through host tools — the same
`COPILOT_GITHUB_TOKEN` a run needs must be on the daemon host.
`[concierge] enabled | model | timeout_s | max_tool_calls | session_turns | github_tools | create_issues | edit_config | config_locked`
tune it (`sbxloop init` documents them; `sbxloop doctor` shows the row).
Plain messages in the control channel are left alone — people talk among
themselves without the bot answering.

Bot setup, once: create an application in the Discord Developer Portal, add
a bot, enable the **Message Content** privileged intent, copy the token, and
invite the bot to your server with View Channel, Send Messages, Create
Public Threads, Send Messages in Threads, Add Reactions, and Read Message
History. Chat is observability, never a dependency: if it is down, the
daemon logs and carries on.

**Slack instead.** `pip install 'sbxloop[slack]'`, set `[slack] channel_id = "C…"` (the channel's *id*, from its details pane — not its name) and put
`SLACK_BOT_TOKEN` (`xoxb-…`, the Web API) and `SLACK_APP_TOKEN` (`xapp-…`,
the Socket Mode connection) in the environment / `.env` — never in
`sbxloop.toml`; they are read from the environment only and never logged.
The app runs in **Socket Mode**, so it dials out and needs no public URL or
request signing — what a daemon on a home server needs. Create the app once
at api.slack.com/apps (from scratch): under *Socket Mode* enable it and
generate an app-level token with `connections:write`; under *OAuth &
Permissions* add the bot scopes `chat:write`, `channels:history`,
`channels:read`, `groups:history`, `groups:read`, `reactions:write`,
`users:read` and `app_mentions:read`; under *Event Subscriptions* subscribe
the bot to `message.channels`, `message.groups` and `app_mention`; install
the app to the workspace and `/invite @your-app` into the control channel.
On Slack's shapes: the run thread is the reply thread under the headline
message (its `ts` is the thread id; `thread_per_run = false` posts everything
top-level), cards are coloured attachments (`[slack] embeds`), link unfurls
are off on every post and edit, reactions use Slack's emoji names, agent
prose is entity-escaped so it can never `<!channel>` anyone, and Slack has
no "reply to a message" outside threads, so the concierge and steering are
@mention-only there (`<@app>` in the control channel or in a run's thread).
`sbxloop doctor` shows one `chat bridge (slack)` row: extra installed, both
tokens present. Switching backends is a config change plus a daemon
restart; runs recorded under the other backend keep their thread rows but
are not re-posted.

**Mattermost instead.** For an instance you host. `pip install 'sbxloop[mattermost]'`, set `[mattermost] url` to the instance (scheme
included — a private hostname and a port are ordinary here) and
`[mattermost] channel_id` to the channel's 26-character id (channel name →
*View Info*, not a `~name`), and put `MATTERMOST_BOT_TOKEN` in the
environment / `.env` — never in `sbxloop.toml`. Create a bot account in the
*System Console* → *Integrations* → *Bot Accounts*, then add it to the
control channel. The bridge connects over a **websocket**, so like Slack's
Socket Mode it dials out: no public URL, no inbound hole, and a daemon
behind NAT works.
On Mattermost's shapes: the run thread is the reply stream under the
headline post (its post id is the thread id; `thread_per_run = false` posts
everything top-level), and Mattermost does not nest, so the one-level
chronology is the shape it already wants. Reactions use the standard emoji
names. Mattermost has no allowed-mentions control, so agent prose is passed
through a mention guard — a zero-width space parks every `@name` that would
resolve, leaving it readable and inert — which is why a run's prose can
never ping `@channel`. Replying on Mattermost means posting in a thread
rather than answering one message, so — as on Slack — the concierge and
steering are @mention-only (`@your-bot` in the control channel or in a
run's thread), and people can talk to each other in a run's thread without
the bot answering. A thread you open under any *other* post in the control
channel — a run's finish notice, a concierge answer, a gate prompt — counts
as the control channel: an @mention there reaches the concierge and is
answered in that thread, and `!sbx` commands work there too. Only a run's
own thread steers. `sbxloop doctor` shows one
`chat bridge (mattermost)` row: extra installed, token present.
Cards are coloured message attachments (`[mattermost] embeds`) — a post the
server rejects is retried text-only, so a run's chronology never goes
missing over presentation — a workload result's files are uploaded up to
`max_attachment_bytes` and five per post (Mattermost's own limit; the rest
are named by host path, as is any file too large or whose upload fails — a
named file is never silently dropped), and the merge gate's approve button
is a seeded ✅: reacting with it approves, exactly as `!sbx merge` does, and
the reaction comes back off once the gate resolves so a merged prompt never
looks like it is still waiting for you.

While the concierge is working on an @mention it shows **"…is typing"**
under the message box, for as long as the turn takes — the same signal
Discord gives, sent over the websocket the bridge already holds, so it
costs no API call. The ⏳ / ✅ reactions on your own message say *received*
and *answered* on top of it. Those marks go by Mattermost's standard emoji
names; if your instance's emoji set does not carry one, the bridge says so
once in the log (`mattermost.reaction_refused`, naming the emoji and what
the server said) rather than leaving you with an ack that silently never
appears.

One Mattermost quirk the bridge works around rather than documents away: the
web and desktop apps drop a reaction on *your own* message if it arrives
before your client has finished posting it. The bot reacts within
milliseconds, your create-post reply comes back carrying no reactions, and
the app takes the reply's word over the one it already had — so the ⏳ is on
the server (everyone else sees it; a reload shows it) while your screen
skips straight to ✅. The bridge puts the ⏳ on a second time about a second
and a half later, which the server re-broadcasts and your client keeps.

### The remote API

[docs/api.md](api.md) is the reference: installation, clients and tokens,
the endpoint catalog, the operation and idempotency contract, the streams,
every error code, the limits, the isolation guarantee and the recovery
procedures. This section is the walk-through.

With `[api] enabled = true` (and the `sbxloop[api]` extra installed) the daemon
also serves a remote operations API in-process: REST under `/v1`, OpenAPI at
`/v1/openapi.json`, liveness at `/health/live` and readiness at `/health/ready`
(503 until recovery has finished — commands are refused, not queued, until
then). Register a client on the host and mint a token:

```bash
sbxloop api client create ci-reporter --cap runs:read --cap audit:read
# client_id:     cli_…        client_secret: sk_…   (shown once)
curl -s -X POST http://127.0.0.1:8420/v1/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"grant_type":"client_credentials","client_id":"cli_…","client_secret":"sk_…"}'
# {"token_type":"Bearer","access_token":"…","expires_in":900,"refresh_token":"rt_…",…}
curl -s http://127.0.0.1:8420/v1/status -H 'Authorization: Bearer …'
```

Capabilities are the spike's (`runs:read`, `runs:control`, `runs:steer`,
`gates:approve`, `budgets:grant`, `daemon:manage`, `audit:read`,
`diagnostics:read`, …); a token carries what its client was granted, narrowed
at once if the grant is narrowed later. Every refusal is
`application/problem+json` with a stable `code`. `GET /v1/status` is the
daemon's live state; `GET /v1/operations` is the record every surface (ctl,
chat, the console, the API) writes when it acts. The listener binds loopback by
default; put your reverse proxy in front for TLS and list it in
`trusted_proxies` ([deploy.md](deploy.md)).

**Reading the work.** `GET /v1/items` (filter by `state`, `kind`,
`repository_id`), `GET /v1/items/{id}` (the request, its origin, every run it
had, who admitted it), `GET /v1/queue` (dispatch's own order — an interrupted
run's resume first, then oldest first — with each entry's eligibility and
whether a pause or the breaker holds the whole queue), `GET /v1/runs` (touched
most recently first; filter by `state`, `kind`), `GET /v1/runs/{id}` and
`/v1/runs/{id}/tasks`, and the catalog: `GET /v1/repositories`, `/v1/profiles`,
`/v1/recipes`. Every collection pages by an opaque `cursor` bound to its
filters (`limit` up to 200). A client sees opaque ids — `itm_…` for a work
item, `run_<run id>` for a run, `repo_…` for a repository — never a bare issue
number, so two repositories' issue 7 are two ids; the item's `origin` names the
repository, the number, the URL and the id the daemon's own surfaces use
(`gh:issue:7`). An unknown id of any kind is a plain `404 not_found`. Each
read carries `available_actions` (what the daemon would accept for it right
now) and a `revision` a command may pin with `expected_revision`.

**Admitting work.** `POST /v1/items` (`items:create`; an `Idempotency-Key`
header is required, and a replay under the same key answers with the same
item and operation, a different body under it `409 idempotency_conflict`)
takes one of three forms, each through the rules its source already applies:

```json
{"kind": "issue", "repository": "you/one", "number": 42, "run_kind": "code"}
{"kind": "workload", "ask": "Summarise the week's incidents", "profile": "research", "sink": "issue"}
{"kind": "tool", "recipe": "entrygraph", "parameters": {"repository": "you/one"}}
```

An **issue** must be open in a configured, enabled repository (by
`repository` or `repository_id`): the daemon reads it and, when it does not
already carry the queueing label for `run_kind` (`trigger_label`, or
`workload_label` for a workload), adds that label exactly as a person would —
so polling and the API converge on one item, and an issue already in progress
or labelled for the other kind is refused by name rather than relabelled. A
**workload** is the same ask the concierge's `start_workload` queues, under a
`[[workloads]]` profile (the `[workload] default` when omitted; a sink the
profile does not allow is refused) and an `api:` item id that the daemon
reports to nobody but its own log and record. A **tool** names a registered
recipe and only the parameters that recipe takes (`GET /v1/recipes` lists
them) — never a command — and `[entrygraph] enabled = false` refuses the one
recipe there is. The reply is `201` with the item and its `item.admit`
operation, `200` when the same work was already queued. `POST /v1/items/{id}/retry`, `/requeue` and `/abandon` (`runs:control`; body
`{"reason": …, "expected_revision": …}`, both optional) are the ctl verbs of
the same names through the same service, each answered with the item as it
stands and the operation that changed it.

**Following the work.** Every public event — the daemon's notices, a run's
start and finish, its engine chronology (every persisted event, `worker.stdout`
included; filter with `type_prefix`), gate transitions, and every operation any
surface recorded — lands in one durable, ordered chronology with an id
(`evt_<n>`) that is also the cursor. `GET /v1/status` reports its `watermark`;
`GET /v1/events?after=evt_<n>` (or `/v1/runs/{id}/events`) pages what came
after it, so a client that reads a snapshot and then subscribes from its
watermark sees no gap and no repeat. `GET /v1/events/stream` is the same
cursor space as server-sent events: resume with `Last-Event-ID`, ignore the
`: ping` comments, and reconnect from the last id when the stream closes (it
does when the daemon stops or the token is revoked; it says why in a
`stream.closed` frame). `/v1/ws` multiplexes the same events and the same
typed commands on one WebSocket — a bearer token in the `Authorization` header
or an `auth{token}` first frame (never the query string), `subscribe{after, run_id, type_prefix}`, and `command{id, action, target, params, idempotency_key, expected_revision}` for `item.admit`, `item.retry`,
`item.requeue` and `item.abandon`, answered by `reply{id, ok, result | problem}` with the same body and the same idempotency the REST route has.
History is kept for `[api] replay_retention_s`; a cursor below what remains is
`410 cursor_expired` with a pointer to the snapshot, never a silent skip. Live
streams are bounded by `[api] max_stream_clients`.

**Steering and deciding.** `POST /v1/runs/{id}/steering` (`runs:steer`; body
`{"text": …, "source_refs": [...], "expected_revision": …}`) hands explicit
direction to the run in flight — the same input path a message in the run's
chat thread takes — and answers `202` with a steering record (`str_…`) whose
status follows what became of it: `delivered` when the run took it,
`handled` with the agent's `reply` and the `action` it took once the reply
lands, `failed` when the daemon refused it, `undelivered` when the run ended
first. `GET /v1/runs/{id}/steering` lists them. Instructions are handed over
in the order they arrive and answered in turn, so conflicting directions are
all heard and the later one is heard last; a run that is not in flight, or a
tool run, is refused by name. `GET /v1/gates` and `/v1/gates/{id}` show the
merge and publication gates with their `revision`, subject (`pull_request`,
`head_sha`) and required authority; `POST /v1/gates/{id}/approve`
(`gates:approve`; `expected_revision` required) endorses exactly the gate the
person saw — a gate that moved is `409 stale_revision`, a second approval `409 already_in_progress` — and commits the release; the merge or publication
completes afterwards and lands as `gate.resolved` in the chronology. A base
that requires an approving review still waits for one: the API's approval is
the daemon's, not the reviewer's. `POST /v1/runs/{id}/cancel` (`202`,
honoured at the run's next boundary; never another run that happens to be
current), `/resume`, `/round-grants` (`budgets:grant`; `{"rounds": n}`) and
`/review-wait/resume` are the ctl verbs of the same names, each with an
optional `expected_revision`. Every one of these is a `command` on the
WebSocket too.

**Results and usage.** `GET /v1/runs/{id}/artifacts` (`artifacts:read`) is
the run's catalog: every file the run left behind by an opaque `art_…` id
with its path inside the run's artifact tree (never a host path), size,
SHA-256 digest, media type, the task that declared it, and where it came from
(`workspace` for a mounted code run's tree, `harvest` for one copied out,
`sink` for what a workload or tool run declared) — the same files `sbxloop artifacts` lists, the operator's `[artifacts] exclude` applied, up to two
thousand per run. The listing also says where the run **published** (its
sinks), which is a separate fact from a file being on the host. `GET /v1/artifacts/{id}` is one entry; `GET /v1/artifacts/{id}/content` its bytes,
always as an attachment (`Content-Disposition`, `nosniff`; HTML, SVG, XML and
scripts are served as plain bytes), opened relative to the run's own
directory without following a link out of it — a link that would escape the
run is never catalogued. Once the retention sweep has pruned a run the entry
stays with `available: false` and the download answers `410 artifact_gone`.
`GET /v1/runs/{id}/usage` and `GET /v1/usage?since&until` (RFC 3339 or epoch;
the daemon's calendar day when omitted; at most 90 days) are what the agent
backend reported — tokens and turns by persona and by phase and model — with
unknowns kept as `null` and `recorded: false` when nothing was reported, which
is not zero. `spend` is always `null`, and `spend_basis` says why: no backend
reports a charge in a known unit, and a token total is not a bill.

**Diagnostics and administration.** `GET /v1/logs` (`diagnostics:read`;
`tail` up to 500, `level`, `grep` as a plain substring) is the daemon's
in-process log ring — the same lines `ctl log` shows — as records, with
anything shaped like a credential (a bearer token, a JWT, a client secret, a
forge token, a `password=` value) masked before it leaves the host.
`GET /v1/configuration` is the configuration this daemon runs on, restricted
to the sections a remote operator may read (`sections` lists them; the
listener's own `[api]`, the chat backends and every key naming a host path or
a credential are left out by design, and an environment variable's *name*
travels while its value never does), each key with the layer it comes from
(`source`), whether a change applies `live` or at the next `restart`,
`pending` when the file on disk now says something else, and `locked` with
the reason the daemon's own tools may not change it. `GET /v1/daemon/holds`
lists the holds standing with whose each is; `POST /v1/daemon/holds`
(`daemon:manage`; `{"name": …, "reason": …}`) takes one attributed to your
client — nothing new is claimed while it stands, the run in flight is not
touched, it survives a restart and no disconnect releases it — and
`DELETE /v1/daemon/holds/{name}` releases yours; another client's is
`409 hold_owned` unless `?force=true` says you are overriding, which the
operation records. `POST /v1/daemon/stop` answers `202` the moment the stop
is durably accepted: nothing new is claimed, the run and landing in flight
finish, then the process exits and this listener with it — streams hear
`daemon.stop_requested` and then `closing`, and a poll of the operation may
find the connection refused; under a service manager that restarts the
daemon this is a restart, and the holds standing now still stand when it is
back. `POST /v1/daemon/restart` (`{"now": true}` cancels the run in flight
first; it is resumable) is that stop under a supervisor that starts the
daemon again, refused as `409 unsupervised` when nothing would — the reply
carries the generation that accepted it, and the daemon is back when
`/health/ready` reports a new one; starting a daemon that is not running is
the supervisor's job, never the API's. `POST /v1/repositories/{id}/resume`
polls a suspended or backing-off repository again now. `GET /v1/schedules`
and `/v1/schedules/{name}` show every schedule with its cadence, last and next
due and who paused it; `POST /v1/schedules/{name}/pause` and `/resume`,
`POST /v1/schedules` (a declared profile, a free name, exactly one of `every`
/ `cron`) and `DELETE /v1/schedules/{name}` are the concierge's own schedule
tools over HTTP, live from the next tick. Every one of these is a `command`
on the WebSocket too (`daemon.hold`, `daemon.release`, `daemon.stop`,
`daemon.restart`, `repository.resume`, `schedule.*`). Not offered remotely, by
design: configuration writes, repository registration, backup and restore,
garbage collection and sandbox deletion stay on the host's own CLI.

## Artifacts

Every job in a run executes in the run's **workspace** — a host directory
(`~/.sbxloop/runs/<run>/workspace`) that sbx mounts into the agent microVM.
Provisioning *discovers* the in-VM mount point (marker file + bounded search)
rather than assuming one. A run that has no checkout to work on (nothing
configured, not started from inside one) uses an empty per-run directory
instead; when *that* mount can't be found, jobs run in a fallback dir that is
**harvested** to `~/.sbxloop/runs/<run>/artifacts` with `sbx cp` at each task
end and at run finalize. A configured checkout that fails to mount stops the
run instead (`sbxloop doctor` has the workspace-mount probe). Either way the files an agent
produces survive the sandbox:

```bash
sbxloop run "write a fib.py with tests"   # summary ends with an artifact tree
sbxloop artifacts <run>                   # list a past run's files (--tree for a tree)
cat "$(sbxloop artifacts <run> --path)/fib.py"
```

Harvest, listings and delivery all skip the same set of path components,
matched at any depth: run/VCS state (`.git`, `.sbxloop`) plus the
regenerable dependency and build trees of the supported languages —
`node_modules`, `__pycache__`, `.venv`/`venv`, `*.egg-info`, the Python
tool caches, `target` (cargo/Maven), `.gradle`, `obj` (.NET), `.bundle`,
`CMakeFiles`. Entries may use glob patterns, matched against whole path
components (`*.egg-info` catches pip's project-named metadata directory).
They are large, reproducible from the manifests that *are* delivered, and
nobody wants them in a delivery PR diff. The ambiguous generic names —
`bin`, `build`, `dist`, `out`, `lib`, `vendor` — are **not** excluded, since
each is build output in one ecosystem and checked-in content in another; add
them to `[artifacts] exclude` if your project wants them dropped. Whatever is
excluded is always counted and reported (`12 file(s) excluded (node_modules)`)
in run summaries, `sbxloop artifacts`, and the delivery PR body — never
silently truncated.

## GitHub integration

GitHub is the first of the version-control backends. `[vcs] kind` names the
forge a repository lives on: `github`; `gitlab`, whose backend answers every
role: the read paths (the repository, issues, checks and the base's rules),
the merge request, its review threads, its landing (the un-draft, the
rebase, the merge, and the merge train where the project has one) and the
remote commit (one changeset through the commits API, see Delivery below);
or `gitea`, which loads ahead of its backend and which
`sbxloop doctor` says is not implemented yet. `sbxloop doctor` prints one
`vcs backend <kind>` row per forge
naming what the backend can do — `supported`, `unsupported` or `unknown`
per capability — and, on each repository's row, the kind of credential its
github box holds and how long it lives. A GitLab token reads its own record,
so that row carries the token's name, scopes and expiry, a token that never
expires earns a soft `credential` row asking for one, and a revoked token
fails the repository's row; a base only Maintainers may merge into, held by
a Developer token, is a merge blocker on the same row, phrased with the
branch's own access description. A repository on another forge takes
one token, in the variable `[vcs] token_env` names (`GITLAB_TOKEN` or
`GITEA_TOKEN` by default); `secrets.env` and `sbxloop.toml.example` carry
commented stubs for both. Everything below is the GitHub backend.

sbxloop has **no** GitHub capability until you name at least one repository
it may work with — either per run on the command line:

```console
$ sbxloop run "build the thing" --repo you/your-repo
```

or persistently in `sbxloop.toml`:

```toml
[github]
repo = "you/your-repo"   # the ONE repo sbxloop may act on
deliver_base = ""        # base branch for the PR; unset uses the repo's default (or `--deliver-base`)
create_repo = false      # create the repo if missing (or `--create-repo`)
create_public = false    # created repos are private unless flipped (or `--create-public`)
```

Several repositories can be registered instead, as an array of tables — each
entry carries its own delivery settings, an `enabled` switch and an optional
per-repo token environment variable:

```toml
[[github.repos]]
repo = "you/one"
workspace = "~/src/one"   # this repo's host checkout; runs clone from it
deliver_base = "main"

[[github.repos]]
repo = "you/two"
workspace = "~/src/two"
enabled = false           # registered but not polled
token_env = "GH_TOKEN_TWO"  # unset uses the daemon-wide GH_TOKEN
trigger_label = "sbxloop:go" # unset uses [daemon] trigger_label
in_progress_label = "loop:wip"  # any lifecycle label can be renamed per repo
labels = ["team:core"]      # extra labels for this repository
```

Every lifecycle label — `trigger_label`, `in_progress_label`, `failed_label`,
`completed_label`, `blocked_label`, `gated_label`, `workload_label` — can be renamed on an
entry; unset ones take the `[daemon]` value, and the seven must stay distinct
(case-insensitively) per repository. Nothing creates the trigger label a
human is told to apply, and GitHub creates the lifecycle labels on first
attach with a random color and no description: **`sbxloop init-repo owner/name`** creates the seven (plus `[landing] followup_label`) with colors
and descriptions up front, idempotently, through one github-ops sandbox —
run it again after renaming a label. `sbxloop doctor` reports missing labels and a
repository whose Issues are disabled as advisory rows; it does not fix
them. Claiming an issue needs a token that can write issue labels
(fine-grained token or GitHub App: Issues → read and write; classic PAT:
`repo`): a triage-only token can read and comment but not label, and the
claim fails with an error that says so instead of a bare 403.

A run's github-ops sandbox is provisioned **scoped to the repository its work
item came from**: it is told which repository it acts on, and it is given that
repository's `token_env` credential — falling back to the daemon-wide
`GH_TOKEN`/`GITHUB_TOKEN` when the entry names none. The credential split is
unchanged by any of this: the GitHub token only ever enters the github-ops
sandbox, never the agent sandbox (which holds the Copilot token alone) and
never the host.

#### A workspace per repository

A **workspace** is the host git checkout a run's tree is cloned from: every
fresh run clones it into `runs/<run_id>/workspace` on its own branch, so the
run never disturbs the checkout, and the daemon fast-forwards it from
`origin` before each run. With several repositories that checkout cannot be
a single daemon-wide path — one repo's runs would be built out of another
repo's tree — so each entry names its own with `workspace`:

```toml
[[github.repos]]
repo = "you/one"
workspace = "~/src/one"

[[github.repos]]
repo = "you/two"
workspace = "~/src/two"
```

**The origin check.** For every enabled repository, the checkout's
`origin` remote must name that repository (`.git` suffix, ssh vs https and
case are normalised away). A mismatch is a hard failure, named at three
points: `sbxloop doctor` reports it as a failing check, `sbxloop daemon`
refuses to start, and provisioning refuses to clone even if it were somehow
reached. The message names both repositories and the fix. Nothing falls
back to another repository's tree, ever — silently building `you/two` from
`you/one`'s checkout is the bug this check exists to prevent.

**No workspace.** An entry with no `workspace` (and no legacy one that
belongs to it) has no host tree, so its runs clone the repository from its
own remote into the run directory (from the server `[github] api_url`
names, single-branch, optionally blob-filtered — see
[Working against an existing checkout](#working-against-an-existing-checkout)).
The clone authenticates with the run's own GitHub credential — the
daemon-wide `GH_TOKEN`, the entry's `token_env`, or a GitHub App
installation token minted on the host — so **private repositories clone
like public ones**. The token reaches git through a one-shot credential
helper that exists only in that clone's environment: it is never on the
command line, never in the clone's `.git/config` or remote URL, and any
credential helper the host user has configured is switched off for that
process, so the host still holds no git credential of its own. With no
GitHub credential configured at all only a public repository can be
cloned; a failure names which case applied, rather than falling back to
anything. `sandbox.workspace_clone` records whether the clone was
authenticated.

**Migration.** A single-repo deployment's `[sandbox] workspace` keeps
working exactly as before. When you add a second repository, **move
`[sandbox] workspace` into the matching `[[github.repos]]` entry** as
`workspace = "..."` and give the other entries their own. Left at the top
level with several repositories configured, it applies only to the entry
whose `origin` it actually matches; every other repository is refused at
`doctor`/start rather than run from the wrong tree.

The two forms are mutually exclusive: migrate by moving `[github] repo` (and
its `deliver_base` / `create_repo` / `create_public`) into one
`[[github.repos]]` entry. A single `[github] repo` keeps working unchanged and
is normalised internally into a one-entry repo list. Everything under
`[[github.repos]]` is **per repository**; the daemon-wide guardrails — the
daily run cap, the per-item retry cap, the consecutive-failure circuit breaker
and one-run-at-a-time — stay global to the daemon. Work items are keyed by
issue number **and** repository, so issue #4 in two registered repositories is
two independent items; an existing daemon state database is migrated in place
on first start. Rows written before the migration carry no repository: when
exactly one repository is configured they are backfilled with it. When
several are, the daemon first names each row from its issue URL
(`store.repo_attributed_from_url`); of what is left, only rows still sitting
untouched in the queue are dropped (logging `store.repoless_items_dropped`)
because only those can be rediscovered — claiming an issue swaps the
`sbxloop:run` label for `sbxloop:in-progress`, so an already-claimed or
in-flight item can **never** be picked up again by discovery. Those rows are
therefore failed rather than deleted, with an operator notice naming each
item id and issue URL (`daemon.repoless_items_stranded`): their issues keep
the in-progress label until a human clears it and re-adds `sbxloop:run`.
Finished items stay as history either way.

`sbxloop config repos` lists the registered repositories with their enabled
state, base branch, token variable and trigger label; `sbxloop doctor` checks
each enabled repository on its own line (a failing repo never masks the
others' verdicts); `sbxloop status` and `sbxloop daemon items` carry a `repo`
column so every run and work item shows which repository it belongs to.
Commands that need one repository — `sbxloop run`, `config repos --repo` —
default to the sole configured repository and, when several are registered,
ask for `--repo owner/name` rather than guessing.

`repo` is the gate, and there is no separate switch behind it: unset, no
github sandbox is provisioned, `GH_TOKEN` is not needed, and a run ends
`completed` after its gate with the work in the workspace. Set, **every run
that passes its gate opens a pull request there and carries it through
review, CI and the merge** — delivery is not an optional step at the end of
a run, it is the second half of one. CLI flags win over the toml, so
`--repo` can also redirect a configured setup at a different repository for
one run.

The repository is probed right after provisioning, so a missing or typo'd
`--repo` fails the run up front instead of after the work is done. For a
fresh project, add `--create-repo` and sbxloop creates it (private by
default, `--create-public` to flip) with an initial commit, then delivers
the work as a normal reviewable PR — creation is opt-in precisely so a
typo'd repo name errors instead of silently landing in a brand-new
repository. Creating repos needs a token allowed to do so for that owner;
the per-repo minimal token suffices for everything else. An
existing-but-empty repository (no commits yet) is also handled: delivery
bootstraps the initial commit itself.

With `repo` set, runs provision the github-ops sandbox and require a second
PAT, `GH_TOKEN`, used *only* by that sandbox. It needs the repository
permissions in [docs/permissions.md](permissions.md) — contents and
pull requests to deliver and merge, issues to claim and settle, checks and
actions to wait on CI and read failed-job logs. Without it, no github
sandbox exists and repo-facing features refuse to run. The PAT can be
replaced wholesale by a GitHub App installation — see
[GitHub App auth](#github-app-auth). `sbxloop doctor --probe` checks the
token against that table before a run can fail on it (#696): a required
permission the token lacks is a failing row naming the permission and the
feature that first needs it, a missing `workflows:write` is a warning
(only a delivery touching `.github/workflows/` needs it — such a delivery
is refused up front with the permission named and the item ends `blocked`
rather than burning retries on GitHub's 403, #752), and a
`github repo <r> ci` row says what Actions the repository has for the CI
stage to wait on — or that it has none.

**Delivery** is one atomic commit via the git data API on branch
`sbxloop/<run>`, opened as a draft pull request, with the harvested tree
filtered by `[artifacts] exclude`. A run clone delivers its `git diff`
against the base — deletions and renames included; a workspace without a
git history delivers a snapshot of the tree. Either way the tree records
what is on disk (#695): an executable script arrives `100755` and a symlink
arrives as a symlink (`120000`, its target as the content), never flattened
to a plain file. On GitLab the same steps are staged in the backend and
written as one commits-API changeset (create, update, delete and chmod;
binary as base64) on a pending `sbxloop/pending/<id>` branch, and the
run's branch is then created at that commit; a pending branch left behind
by a delivery that died mid-way says what it is by name and the next
delivery does not depend on it. A submodule pointer and a symlink are
refused by name on GitLab: the commits API has no action that writes
either. Every fix round re-delivers onto the same
branch — force-moved, the open PR reused — so one run is one PR, and
`sbxloop resume <run>` at `delivering` is the retry path when a delivery
failed. On GitLab a fix round does not move the branch (GitLab has no
call that does, and deleting the branch closes its open merge request):
it writes the same changeset from the same parent onto the branch again
under `force`, a new commit of the same tree, which the merge request
follows. The PR's description is the repository's own pull request template
(`.github/PULL_REQUEST_TEMPLATE.md` and the other places GitHub reads it
from) verbatim, followed by the loop's summary — so a check that parses the
template sees its sections — and the planner is told the template exists
and that the last task should write it filled in to `.sbxloop/pr-body`
under the workspace, which then *is* the description (read, never
delivered); a fix round can rewrite the description the same way when a
check judges it. `Closes #N` is always the last line. The PR stays a draft until the review approves and CI is green, so a
watching human reads "draft" as "sbxloop is still working on this". A
repository plan without draft pull requests (GitHub answers the draft with
a 422 saying so) gets a ready PR on one retry, logged
`deliver.draft_unsupported`; the run is otherwise unchanged. The loop clears
only the draft it made: a PR *a person* converts to draft — before the
landing, or after the loop's own un-draft — is a hold, not a block (see
`awaiting_review` in the daemon section).

**Repository conventions.** What the repository says about itself reaches
every phase: `AGENTS.md`, `CLAUDE.md`, `.cursorrules`,
`.github/copilot-instructions.md`, `CONTRIBUTING.md` and `CODEOWNERS` (the
locations GitHub reads them from) are read from the workspace and handed to
the planner, the builder and the reviewer under one heading — "Repository
conventions (from the repository itself — follow them over the defaults
below)" — so "run `make lint` before committing", "never touch
`generated/`" or "PRs need a changelog entry" shape the plan and the review,
not just the build. A file symlinked or copied under two names is rendered
once. The block is capped at `[budgets] repo_context_max_chars` (12,000
characters; the cut is marked, and 0 hands the prompts none of it). Neither
agent backend is left to find these files by its own convention: the block
is the one route, the same on both.

**Naming.** The branch, the PR title and the commit message are rendered
from `[github]` templates — `branch_prefix` (default `sbxloop/`, the run id
appended), `pr_title_template` (default `sbxloop: {title}`) and
`commit_message_template` — with `{title}`, `{outcome}`, `{run_id}` and
`{repo}` as placeholders; each can be overridden per `[[github.repos]]`
entry, so a repository with a title lint, a commit lint or a branch ruleset
gets names it accepts. `{title}` is the plan's own `pr_title`, written in
the repository's commit style (the decomposer is shown the recent `git log`), falling back to the run's outcome. A fix round can retitle the PR
by writing `.sbxloop/pr-title` in the workspace — the file is read, never
delivered — so a red title-lint check is curable like any other; a
re-delivery whose title changed renames the PR. A repository that lints
titles as conventional commits — a `commitlint.config.*` or
`.commitlintrc*`, a `commitlint` key in `package.json`, a workflow running
`amannn/action-semantic-pull-request` or `wagoid/commitlint-github-action`
— is detected from the tree: the planner is told to write `pr_title` as
`type(scope): summary`, and when `pr_title_template` is the default the PR
gets the bare conventional title instead of `sbxloop: {title}` (a title
without a type becomes `chore: …`, lowercased; a template the operator
wrote is left alone). A branch creation GitHub refuses (422) fails the delivery quoting GitHub and naming the knob its
wording points at: a branch-name or creation rule → `branch_prefix`; a
signature rule → signed commits, satisfied by a GitHub App credential; a
locked or archived repository → nothing to configure; wording the loop
does not recognise names no knob, so a guess never sends anyone to the
wrong setting.

**The review is the run's own.** A fresh read-only session reads the diff
and returns a verdict; the run acts on that verdict whatever GitHub does
with it. It is also posted to the PR for the record. **Single-identity
mode** is the common case: one token opens the PR *and* reviews it, and
GitHub refuses `REQUEST_CHANGES` / `APPROVE` from a PR's own author — so
when the PR's author is the loop's login, the review is posted as PR
comments instead of through the review feature: each anchored finding as
its own review comment (a thread that can be replied to and resolved, which
is what reconciliation does in later rounds), and the verdict — in words,
`**Review verdict: changes requested** (round 2)` — with the summary and
any finding that got no thread (no line, over the inline cap, or an anchor
GitHub refused, degraded per finding rather than per review) in one
top-level comment. No review-feature call is attempted, so a round costs no
422s. When a *different* identity reviews (a second token), the verdict is
posted as `APPROVE` / `REQUEST_CHANGES`, falling back to a `COMMENT` review
if the repository refuses it. Neither gates anything on GitHub's side, which
is fine: the gate is in the run. A *human's*
standing `REQUEST_CHANGES` on the PR is honoured — it costs a fix round on
the CI budget — and a human merging the PR themselves is the acceptance,
while a human closing it unmerged fails the run.

**Bots that review.** A GitHub App reviewing the PR (CodeRabbit, Copilot,
Sourcery…) leaves a `CHANGES_REQUESTED` it never dismisses. That is a
signal, not a veto: it buys **one** dedicated fix round with the bot's
findings in the brief and a reply on each of its threads, and a bot review
still standing afterwards is merged over and named in a PR comment — it
never blocks the landing. A person's review keeps full authority, and a
person beside a bot still wins. Who is a bot is read from GitHub
(`user.type`, `author.__typename`), never guessed from a name;
`ignore_reviewers` adds User-type accounts to treat the same way (a
reviewer bot on a personal token). There is no reverse list.

**Whose red is it.** A red check on the delivered head is judged against
the commit the PR is built on (its merge base with the base branch, never
the base's current head) and against what the base's protection and
rulesets require. Red on the base too is *preexisting*: merged over and
named in a PR comment, never fixed — unless the base requires that check,
in which case it is fixed (GitHub would refuse the merge) and the fix brief
says the failure was inherited. Red only on the PR is a *regression*: a
required one gets the full `max_ci_rounds`; one the base does not require
gets one round and is then merged over and named, so a signal no human
demanded never blocks a landing. Absent from the base, or a baseline that
could not be read, counts as the PR's own. A base that declares no required
checks gates on all of them. `required_checks` names the gating set
explicitly; `ignore_checks` drops a check everywhere.

Classic branch protection is readable only by an admin, and an
organization's bot usually has write, not admin. When the base's rules
cannot be read, the required set is taken from the pull request itself:
GitHub marks each check on the PR's head as required or not, evaluated
against the very rules the token cannot read, and serves that with pull
access. It is re-read on every poll (only checks that have reported are
listed), the `ci.status` / `landing.checks` events say `source = "pr-rollup"`, and `sbxloop doctor` says on the repository row that the
checks will come from the PR. Only when that is unreadable too does the
loop fall back to gating on every check.

**A workflow awaiting approval** — a check at `action_required`, which is
how GitHub holds a first-time contributor's or a fork's workflow until a
maintainer approves the run — is neither a failure nor something to wait
out: the run ends `blocked` at once, naming the check and the approval it
needs, with no fix round spent. A real red beside it is fixed first.

**Branch protection.** "Require branches to be up to date" is handled: the
landing stage calls update-branch (bounded by `merge_update_attempts`, each
one API call) and re-judges the new head. A rule the loop cannot satisfy —
signed commits or approval of the last push, say — shows as a
`blocked` mergeability once the checks are green, or as a 405 on merge,
which no retry fixes; the run ends `blocked` with the PR open and out of
draft for a human, and the reason is read from the base's rulesets and
classic protection in full — one line per rule the loop cannot satisfy:
approval of the last push (never satisfiable: the loop is always the last
pusher), signed
commits (satisfied by a GitHub App credential, whose API commits GitHub
signs), a linear-history rule against `merge_method = "merge"`, a
required deployment. The `run.blocked` event carries the same list, and
`sbxloop doctor` reports it per repository before any run. When the *only*
unmet rules are review rules — N approving reviews, a CODEOWNERS review —
the run does not block at all: it parks `awaiting_review` and waits for
the human (see the daemon section). A **merge queue** is not a blocker
either: on a base that merges through one, the loop never sends the merge
itself — where the merge would happen, every other bar cleared, it
enqueues the PR (`land.enqueued`) and polls the queue every
`ci_poll_interval_s` until the queue merges it, removes it, or
`ci_timeout_s` runs out. A removal whose checks failed on the queue's own
merge commit is one CI fix round with those checks named (the usual
`max_ci_rounds` budget); a removal with nothing to fix — a human dequeued
it — ends the run `blocked`; a timeout leaves the PR in the queue and says
so.
Conversation resolution is not a blocker: the loop resolves the threads it
answers. A 409 is a race with a push that landed since; the next poll
re-judges.

**Merge method.** `merge_method = "auto"` (the default) takes the first of
squash, merge, rebase that the repository's settings allow, resolved once
per landing and logged. An explicit method the repository disallows is
never swapped for another: `sbxloop doctor` says so on the repository row,
and a run reaching the merge ends `blocked` naming it.

The post-build stages are configured under `[landing]`, effective only with
a repository:

```toml
[landing]
deliver_draft = true            # the PR opens as a draft; un-drafted once review and CI pass
max_review_rounds = 3           # how many times the review may request changes
max_ci_rounds = 2               # rounds for the mechanical failures: red gate, red CI, conflict, human objection
retry_rounds = 2                # daemon: an exhausted run resumes its own PR once with this many more; 0 hands it to a human
ci_poll_interval_s = 60         # how often the delivered head's check runs are polled
ci_settle_s = 90                # "no check runs yet" must persist this long to mean "this repo has no CI"; calibrated to Actions' registration latency — raise for CI that registers later
ci_timeout_s = 3600             # per wait, not charged to max_wall_clock_s; exceeding it ends the run blocked
merge_method = "auto"           # auto | squash | merge | rebase — auto: the first the repository allows
delete_branch_on_merge = true
merge_update_attempts = 3       # update-branch calls when protection wants "up to date"; 0 disables
required_checks = []            # the checks that gate the merge; empty = what the base's protection/rulesets declare, else all
ignore_checks = []              # fnmatch patterns never waited on, fixed or reported (e.g. "codecov/*")
ignore_reviewers = []           # User-type logins treated as automated reviewers: one fix round, never a block
followups = "issues"            # after the merge, the review's out-of-scope notes: issues | comment | off
followup_label = "sbxloop:follow-up"  # never the trigger label — a human promotes a follow-up to work
max_followups_per_run = 5
review_diff_max_chars = 150000  # the diff shown inline to the reviewer; past it, the reviewer reads the tree
```

Landing is not optional and has no off switch: a run with a repository
either merges, ends `failed` with its PR still a draft, or hands a `blocked`
PR to a human. On a repository whose merges publish — sbxloop's own releases
to PyPI and redeploys the daemon host on every merge to `main` — every
merged run is therefore an unattended release. That is the existing pipeline
working as designed, with nobody in front of it; the round budgets and the
daemon's guardrails are what you are trusting instead.

### GitHub App auth

The github-ops side can authenticate as a **GitHub App installation**
instead of a PAT (#568): create a GitHub App with the repository
permissions in [docs/permissions.md](permissions.md) (Contents, Pull
requests and Issues read & write; Checks and Actions read; Workflows read
& write if runs may edit workflow files), install it on the repository, and
configure

```bash
GITHUB_APP_ID=12345                         # the App's numeric id
GITHUB_APP_INSTALLATION_ID=987654           # from the installation's settings URL
GITHUB_APP_PRIVATE_KEY_PATH=~/keys/app.pem  # or GITHUB_APP_PRIVATE_KEY (PEM inline)
```

in the environment / `.env`, leaving `GH_TOKEN`/`GITHUB_TOKEN` unset. The
host signs a short-lived App JWT with its own `openssl`, exchanges it for a
~1 hour **installation token**, and delivers only that token to the
github-ops sandbox; the private key never leaves the host, and the agent
sandbox still sees no GitHub credential. Every operation — issue claims,
comments, labels, PRs, reviews, merges — is attributed on GitHub to the
app (`<app-name>[bot]`) rather than to a personal account, with no
personal-token expiry to babysit.

That attribution is also how the loop tells its own review threads and
reviews from a person's. The identity comes from the credential — the
App's slug, or `GET /user` on a PAT — and carries whether it is an App,
so a person whose login happens to be `foo` is never mistaken for the
App `foo[bot]` (GitHub lets both exist; the two spell the same once the
`[bot]` suffix REST adds and GraphQL omits is folded). When neither
source can answer — a fine-grained token that cannot call `GET /user` —
set `[github] bot_login` (per repository in `[[github.repos]]`) to the
login GitHub attributes the loop's writes to; the delivered PR's author
is a last resort only because the same credential opened it. A
reconciliation reply counts only when the loop wrote it: a person
quoting the loop's marker back does not make a thread answered.

Tokens refresh themselves: before each github job the loop re-mints when
less than ten minutes of lifetime remain and rewrites the sandbox's env
file, so long runs and the daemon's long-lived polling sandbox never hit
an auth failure mid-flight. The mode is chosen by which credentials you
set: configuring **both** a PAT and App credentials — or an incomplete App
set — is a startup error that names the fix, raised before any microVM
boots. A `[[github.repos]] token_env` stays an explicit per-repo PAT
choice and wins over App credentials for that repository. (App JWTs are
signed with the host `openssl` binary; `sbxloop doctor` checks it is on
PATH.)

## Debugging failed runs

By default sandboxes are torn down at run end — including failed runs, which
is exactly when the in-sandbox evidence (worker stderr, install leftovers,
workspace state) matters most. Two levers:

```toml
# sbxloop.toml
keep_on_failure = true   # keep the pair alive only when a run fails (or --keep-on-failure)
keep_sandboxes = true    # keep it always (or --keep-sandboxes)
```

A failed run then ends with a prominent hint naming the kept sandboxes, and
`sbxloop shell` drops you inside — kept, in-flight, or leaked:

```bash
sbxloop shell <run>                    # interactive shell in the agent sandbox
sbxloop shell <run> --role github      # ... or the github-ops sandbox
sbxloop shell <run> -c 'cat ~/.sbxloop/env.sh'   # one-off command
```

Attaching to an in-flight run is meant as observation — the worker owns its
env files and workspace, so avoid mutating them mid-phase. Kept runs are
marked in the state DB (`kept_reason`) and stay exempt from `sandbox prune`
until you pass `--include-kept`, so debugging convenience cannot become a
permanent leak.

One transcript signature worth knowing: agent `glob`/`grep` calls failing
with `<jemalloc>: Unsupported system page size` mean the guest's page size
is not the 4 KiB the Copilot CLI's bundled ripgrep was compiled for (16 KiB
guests are common on Apple-silicon hosts). sbxloop handles this
automatically — the worker reroutes glob/grep to a system ripgrep
(`USE_BUILTIN_RIPGREP=false`) and provisioning apt-installs `ripgrep` on
such guests — so seeing the abort means the fallback had no `rg` to land
on: look for a `sandbox.tooling_warning` event in `sbxloop logs`, and check
the `page-size` probe under `sbxloop doctor --deep`.

### GlitchTip error reporting

sbxloop can report host failures to your own [GlitchTip](https://glitchtip.com/sdkdocs/python/)
project through the Sentry Python SDK. Reporting is off until you put the
project's DSN in `~/.sbxloop/config/secrets.env` (or export it in the host environment):

```dotenv
GLITCHTIP_DSN=https://your-public-key@errors.example.com/1
```

Restart the daemon after changing this file. Real environment variables take
precedence. To label the deployment or use another variable name, add this to
the home's `config/sbxloop.toml`:

```toml
[telemetry]
dsn_env = "GLITCHTIP_DSN"
environment = "production"
log_fields = "diagnostic"
```

Reports include unhandled CLI exceptions, sbxloop ERROR events, and WARNING
events logged with an exception. They carry the sbxloop release, deployment
label, static event name, exception types and messages, chained exceptions,
exception-group members, and stack filenames, paths, functions, line numbers,
and source context. Recognizable credential patterns are redacted using the
same filter as local logs. Local variables and command arguments are not
collected. Messages and source context can include application data; configure
a reporting destination appropriate for that data. Individual text values are
limited to 100,000 characters by the SDK.

#### What an exception-less error report carries

Most ERROR events are not exceptions: a circuit breaker opening, a work item
abandoned, a sandbox that could not be provisioned. They reach the server with
no traceback, so on their own they are a bare event name and nothing to act on.
Three things travel with them instead:

- **The call site**, as the report's culprit (`sbxloop.daemon.loop in _handle_failure`) — the same class of fact a traceback frame carries, so two
  call sites that log the same event name no longer read alike.
- **The event's `hint`**, the static sentence the call site writes for an
  operator reading the journal, as the report's title line
  (`breaker.opened: <hint>`). It is this repository's own prose, identical on
  every occurrence, so a report that explains itself still groups with the
  others of its kind.
- **The record's structured fields**, as far as `log_fields` allows.

`log_fields` decides how much of the record travels, because a log field can
hold a target repository's content:

| Value        | What travels                                                                                                                                                                                                |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `none`       | Nothing of the record: the event name and the call site alone, as before this setting existed.                                                                                                              |
| `diagnostic` | The default. Numbers and flags (attempt counts, durations, cooldowns, failure streaks), the static `hint`, and enum-valued keys sbxloop writes itself (`role`, `backend`, `kind`, `stage`, `state`, …).     |
| `all`        | Everything above plus the free-text fields — `reason`, `error`, item ids, branch names, urls — which say *which* run failed and why. Choose this only for a reporting server that may hold repository text. |

Under every value, fields whose key names a credential are already masked by
the same filter local logs use, free-text values are scrubbed for credential
shapes and trimmed to 2,000 characters, and nothing the SDK itself would add
(breadcrumbs, request, user, server name, module list) is ever sent.

Reports upgrading from an earlier version regroup once: adding the culprit and
the hint changes the fingerprint the server derives, so existing issues for
these events stop receiving new occurrences and a new issue opens in their
place.

The SDK is confined to the host: no DSN is injected into sandboxes and no
sandbox egress rule is added. Session tracking, tracing, profiling, metrics,
breadcrumbs, automatic integrations, and log export are disabled. Unhandled
background-thread exceptions and third-party library logs are not collected;
background failures must reach sbxloop's logging pipeline. Invalid DSNs produce
a `telemetry.init_failed` warning without printing their value, and reporting
failures do not stop a run. CLI shutdown allows up to two seconds to flush.
Remove or empty the DSN to disable reporting, then restart the daemon.

| Key                     | Default         | Meaning                                                                   |
| ----------------------- | --------------- | ------------------------------------------------------------------------- |
| `telemetry.dsn_env`     | `GLITCHTIP_DSN` | Name of the environment variable containing the DSN; never the DSN value. |
| `telemetry.environment` | `production`    | Deployment label attached to reports.                                     |
| `telemetry.log_fields`  | `diagnostic`    | How much of a log record a report carries: `none`, `diagnostic`, `all`.   |

These are host settings, with no per-repository override; tracked project
configuration cannot change the reporting destination.

## Language toolchains

The agent builds a project inside its sandbox, so whatever that project needs
to compile has to be there. Toolchains are installed before the agent's first
turn, instead of the agent discovering a missing compiler on its first build
and spending revision budget on it. Which ones is resolved once per run:

1. `[sandbox] languages`, when set — the operator's choice, never
   second-guessed.
2. Otherwise, **what the workspace declares**: a `go.mod` selects `go`, a
   `package.json` selects `javascript`, a `Cargo.toml` selects `rust`, and so
   on through the manifests in the table below. The root and two levels of
   subdirectories are read (so a monorepo's `packages/<name>/` count;
   `node_modules`, `vendor`, and dot-directories do not), and every match is
   selected — a repo carrying both `pyproject.toml` and `package.json` gets
   both. A manifest that is not valid UTF-8 (a latin-1 author name)
   is decoded leniently — it still selects its language and its version
   pin is still read — rather than failing the provision.
3. Otherwise `python`, so a workspace with no recognizable manifest behaves
   as it always has.

The run log records the answer and its provenance as a `sandbox.languages`
event (`source` is `config`, `detected`, or `default`, and `signals` names the
manifests that matched), so "why did this run install Go?" has an answer.

**Which series** a toolchain provisions is read from the workspace too. Python
honours `.python-version` (an exact pin) and then `[project] requires-python`
in `pyproject.toml` (a PEP 440 specifier); Node honours `.nvmrc` /
`.node-version` (a major, a full version, or an `lts/<codename>` alias) and
then `engines.node` in `package.json` (a node-semver range). .NET honours
`global.json` — the `sdk.version` band together with its `rollForward`
policy, exactly as the SDK applies it, so a `8.0.400` pin under the default
`patch` policy provisions the pinned 8.0.4xx SDK and refuses a 9.x one.
Java honours `.java-version`, `.sdkmanrc`, `.tool-versions` and a Gradle
`toolchain { languageVersion }` / `sourceCompatibility` as an exact major,
and `maven.compiler.release` / `java.version` in `pom.xml` as a floor (a JDK
21 compiles `--release 17` sources; only a level above the default forces a
newer JDK). Ruby honours `.ruby-version`, `.tool-versions` and the Gemfile's
`ruby` requirement (an exact release, or a RubyGems range from which the
highest installable series is taken).

The rule is the same everywhere: the default series when it satisfies the
declaration, else the highest series this host can install that does. A
declaration no series satisfies **stops the run at resolution**, before any
microVM, with a `toolchains.version_unsatisfiable` error naming the file,
the constraint and the installable series — never the default with a
warning, because a project that pins its runtime refuses the wrong one at
the gate (`dotnet` and `bundler` both hard-fail), and a run that spends its
turns finding that out is worse than one that never starts. Widen the
declaration or pin an installable series. A declaration this host cannot
read at all (`jruby-…`, a `graalvm` alias, a non-JSON `global.json`) is
treated as undeclared, with a `toolchains.version_unreadable` warning.
Every choice is a `sandbox.toolchain` event naming the `series`, its
`source` (the file read, or `default`) and the `constraint` it was read
from — so a probe failure is read against the interpreter the project asked
for. Go needs none of this: its own `toolchain` directive in `go.mod` makes
`go` fetch what the module declares.

A pinned Ruby is compiled from source with `ruby-build` (there is no
official binary), which takes several minutes and is the one install with
its own budget (30 minutes) rather than the provisioning default. A project
that pins Ruby is the strongest case for `sbxloop bake`: bake it once and
every run probes the compiled interpreter instead of rebuilding it.

```toml
[sandbox]
languages = ["python"]   # optional; unset = detect from the workspace
```

| Value        | Also accepts               | Detected from                                                                                      | Installs                                                                                                                     |
| ------------ | -------------------------- | -------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `python`     | `py`, `python3`            | `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements.txt`, `Pipfile`, `uv.lock`, `poetry.lock` | `python3-venv`, `python3-pip` (apt), `uv` + Python 3.13 by default (3.8–3.14 by declaration)                                 |
| `cpp`        | `c`, `c++`, `cxx`, `c-cpp` | `CMakeLists.txt`, `meson.build`, `configure.ac`                                                    | `build-essential`, `cmake`, `ninja-build`, `pkg-config` (apt)                                                                |
| `ruby`       | `rb`                       | `Gemfile`, `Rakefile`, `*.gemspec`                                                                 | `ruby-full`, `ruby-dev`, `bundler`, `build-essential` (apt) by default; 3.1–4.0 by declaration, compiled with `ruby-build`   |
| `java`       | `jdk`, `jvm`               | `pom.xml`, `build.gradle[.kts]`, `settings.gradle[.kts]`                                           | `openjdk-21-jdk`, `maven` (apt) by default; JDK 8/11/17/25 by declaration (Temurin tarballs), plus `JAVA_HOME`               |
| `php`        | —                          | `composer.json`                                                                                    | `php-cli` + mbstring/xml/curl/zip (apt), Composer (pinned)                                                                   |
| `javascript` | `js`, `node`, `nodejs`     | `package.json`                                                                                     | Node 24 + npm/npx by default (18/20/22 by declaration; pinned tarballs from `nodejs.org`), plus `pnpm`/`yarn` corepack shims |
| `typescript` | `ts`                       | `tsconfig.json`                                                                                    | `tsc` from npm, on top of `javascript`                                                                                       |
| `bun`        | —                          | `bun.lock`, `bun.lockb`                                                                            | bun (pinned, from npm; the `packageManager` pin by declaration), on top of `javascript`                                      |
| `go`         | `golang`                   | `go.mod`                                                                                           | Go toolchain (pinned tarball from `go.dev`)                                                                                  |
| `rust`       | `rs`, `cargo`              | `Cargo.toml`                                                                                       | cargo, rustc, rustfmt, clippy (pinned rustup)                                                                                |
| `dotnet`     | `csharp`, `c#`, `net`      | `global.json`, `Directory.Build.props`, `*.sln`, `*.csproj`, `*.fsproj`                            | .NET SDK 10 by default (8/9 by `global.json`; pinned builds from Microsoft), plus `DOTNET_ROOT`                              |
| `make`       | `gnumake`                  | `Makefile`, `makefile`, `GNUmakefile`                                                              | `make` (apt)                                                                                                                 |
| `just`       | —                          | `justfile`, `.justfile`, `Justfile`                                                                | just (pinned release binary from GitHub)                                                                                     |
| `task`       | `go-task`, `taskfile`      | `Taskfile.yml`, `Taskfile.yaml`                                                                    | Task (pinned release binary from GitHub)                                                                                     |

Selecting an entry also selects what it is built on — `languages = ["typescript"]` provisions the Node runtime first, then `tsc`.

The three task runners are entries because the gate detector emits their
commands: `make check` on a Go repo fronted by a Makefile needs `make`, and
no sandbox has `just` or `task` unless something installed it. A manifest
selects the runner like any other entry — whether or not it declares a gate
target, since the agent runs `make build` too.

The `javascript` entry covers the package managers too. `corepack enable`
puts `pnpm` and `yarn` shims on PATH; each shim runs the version the
workspace's `package.json` `packageManager` field pins (a lockfile alone
selects the client with corepack's default version), fetched from the npm
registry on first use. `bun` is not a corepack client, so it is an entry of
its own, selected by its lockfile. The verify-command lint requires the
project's scripts to run through the client the lockfile names (`pnpm run …`, `yarn run …`, `bun run …`), and a bare JavaScript dev binary (`eslint`,
`jest`, `vitest`, `tsc`, `prettier`, `mocha`) is rejected in favour of
`npx --no-install <bin>` or the package.json script — a bare binary resolves
to whatever global the sandbox carries, not the version the project pins.

The `python` entry is uv-aware: when the workspace carries a `uv.lock`, the
prompts steer the agent to `uv sync` / `uv run …` instead of a hand-made
`.venv`, and the verify-command lint requires `uv run` heads there (a
`.venv/bin/pytest` beside a lockfile does not carry a uv workspace's own
members). Without a lockfile the `.venv/bin/…` convention is unchanged.
`sbxloop doctor --deep` reports the template's own `python3` against the
pinned series in the `python-version` row.

Three rules apply to every entry. Provisioning is **probe-first** — a template
that already ships the toolchain costs no install and no network. It is
**never fatal** — a failure warns with the toolchain named and the run
continues, since the agent has passwordless `sudo apt-get` as an escape
hatch. And it is **selected, not accumulated** — an explicit `languages`
replaces detection rather than adding to it, so nothing is installed for a
language you did not ask for. Two riders sit outside the selection. The
baseline tools — `git` (#252) and `yq`/`jq` (#751) — land on every agent
sandbox whatever the language set, because tests shell out to git and
YAML turns up in every repository (workflows, compose files, Helm charts)
while no language toolchain ships a parser; the agent is told `yq` is the
jq-syntax one. And `git-lfs` (#693): not a
language and not selectable, it is added to whatever set was resolved
whenever a `.gitattributes` in the workspace routes files through
`filter=lfs`, so the sandbox can read and write the assets the repository
keeps there. Heavier toolchains are better baked into a template
(`sbxloop bake`) than downloaded per run.

### Installer hosts are allowed for the selected toolchains

The apt-only entries (`cpp`, `make`) need only the apt mirrors, which are in
the sandbox's always-reachable baseline; `ruby` and `java` are apt-only at
their default series and download only for a declared one, but the allowlist
is computed before the workspace is read, so their hosts are always allowed.
The rest download from a vendor or registry, and **provisioning runs before any task**, so a task's
`egress` declaration is too late to help it. Each toolchain therefore carries
its installer hosts, and the agent sandbox is created with the hosts of the
*selected* toolchains allowed — under a default-deny sbx preset too. A
language that was not selected opens nothing, and `[policy] deny` still wins
over an installer host (the toolchain then fails to provision, loudly).

| Language     | Allowed at provisioning time                                                                       |
| ------------ | -------------------------------------------------------------------------------------------------- |
| `python`     | `github.com`, `release-assets.githubusercontent.com`                                               |
| `ruby`       | `github.com`, `codeload.github.com`, `release-assets.githubusercontent.com`, `cache.ruby-lang.org` |
| `java`       | `api.foojay.io`, `github.com`, `release-assets.githubusercontent.com`                              |
| `php`        | `getcomposer.org`                                                                                  |
| `javascript` | `nodejs.org`, `registry.npmjs.org`                                                                 |
| `typescript` | `nodejs.org`, `registry.npmjs.org`                                                                 |
| `bun`        | `nodejs.org`, `registry.npmjs.org`                                                                 |
| `go`         | `go.dev`, `dl.google.com`                                                                          |
| `rust`       | `static.rust-lang.org`                                                                             |
| `dotnet`     | `builds.dotnet.microsoft.com`                                                                      |
| `just`       | `github.com`, `release-assets.githubusercontent.com`                                               |
| `task`       | `github.com`, `release-assets.githubusercontent.com`                                               |
| `git-lfs`    | `lfs.github.com`, `github-cloud.githubusercontent.com`, `media.githubusercontent.com`              |

`sbxloop bake` allows the same hosts for the configured `languages` (there is
no workspace to detect from at bake time) and installs those toolchains into
the template. A run on a prebaked template probes its own resolved set in one
shot and provisions only what the template lacks, under the run's allowlist.

Without them the install warns and the run continues — the agent falls back to
bootstrapping the toolchain itself, which is the behavior these entries exist
to improve on, not a broken run. Baking the toolchain into a template
(`sbxloop bake`) sidesteps the per-run download entirely.

## Sandbox hygiene

Sandboxes are torn down at run end, and an in-process registry also cleans up
on Ctrl-C/SIGTERM — but a host crash or `kill -9` can still leak a run's
microVM pair. `sbxloop sandbox prune` garbage-collects those orphans by
cross-referencing `sbx ls` against the state DB:

```bash
sbxloop sandbox prune            # dry run: classify every sbxloop sandbox
sbxloop sandbox prune --force    # actually remove the orphan candidates
```

A sandbox counts as an orphan candidate when its run is terminal
(merged/completed/failed/blocked/cancelled), unknown to this working copy's state DB, or
non-terminal but silent past `--min-age` (default 1 hour — the persisted event
stream, heartbeats included, is the liveness signal). Sandboxes deliberately
kept for debugging are excluded unless you pass `--include-kept`. `sbxloop doctor` reports the current orphan-candidate count.

Run directories accrete too: every run leaves `~/.sbxloop/runs/<run>/` — a
full clone of the target checkout under workspace isolation, plus harvested
artifacts — and an always-on daemon fills the disk with them. The daemon
sweeps them on start and once a day (`[daemon] prune_runs_after_days`,
default 14; `0` disables), and `sbxloop gc` runs the same policy by hand:

```bash
sbxloop gc --dry-run             # classify every run directory, remove nothing
sbxloop gc                       # remove those past the retention window
sbxloop gc --older-than 3        # a tighter window for this sweep only
```

Only terminal runs (merged/completed/failed/blocked/cancelled) past the window go, and never
one whose sandboxes were kept or whose delivery failed — that directory is the
only copy of the work until it is fetched or redelivered. The SQLite rows stay
(they are the audit trail); each removal is recorded as a `daemon.gc` event on
the run, and `resume` refuses a run whose workspace is gone. Fetch results
back within the retention window — the finish summary prints it.

## Setup

1. Build the home: `curl -fsSL …/scripts/install.sh | sh` (see
   [Quickstart](#quickstart)), or `sbxloop init` from an existing install.
   It installs [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/)
   under the home too; then `sbx login` and `sbx policy init balanced`.

2. Create a fine-grained GitHub PAT:

   - `COPILOT_GITHUB_TOKEN` — personal account, **Copilot Requests**
     permission. Used *only* by the agent sandbox.

   Export it, or put it in `~/.sbxloop/config/secrets.env` — the file
   `sbxloop init` wrote from `.env.example`, read by sbxloop itself; real
   environment variables always win. The config is
   `~/.sbxloop/config/sbxloop.toml`, written from `sbxloop.toml.example`
   (every key, commented, with its default; `sbxloop init --stdout` prints
   it). `.env.example` names every credential and `SBXLOOP_*` override the
   code reads.

3. **Optional** — configure the [GitHub integration](#github-integration)
   (adds the second credential: the `GH_TOKEN` PAT, or
   [GitHub App auth](#github-app-auth)).

4. `sbxloop doctor` verifies all of it and prints remediation for anything
   missing.

### Doctor and the sbx conformance suite

Every empirically-learned assumption sbxloop makes about sbx semantics
(secret visibility under `exec`, `cp` directory semantics, workspace-mount
discovery, custom-secret keying, whether `secret set-custom` has grown a
stdin path yet, …) is a named probe with a machine-checkable verdict, cached
per `sbx` version. `sbxloop doctor` runs the cheap probes and serves
live-sandbox verdicts from the cache; `sbxloop doctor --deep` boots one
scratch sandbox for the full suite. When an sbx upgrade flips a verdict that
sbxloop's behavior depends on, doctor warns loudly and names the dependent
behavior. Ordinary runs feed the same cache, so verdicts stay fresh for free.
`sbxloop doctor --fail-on-drift` turns that warning into an exit code (any
drifted, errored, or unprobed probe fails) — the CI e2e lane uses it, and the
scheduled `sbx-conformance` workflow runs it against the newest sbx release
ahead of adoption. Under the copilot backend doctor also checks the
installed Copilot SDK's permission-kind vocabulary against the
field-verified snapshot backing the read-only critic barrier.

### Secret registration hygiene

sbx keys custom secrets by env var name (one registration per var, whatever
the scope), so leftover registrations from old runs or old versions surface
as `already exists in scope …` collisions. Provisioning recovers
automatically, and `sbxloop secrets` manages the same state proactively:

```bash
sbxloop secrets list             # registrations + pre-collision warnings
sbxloop secrets clean            # dry-run removal of stale entries (--apply to execute)
sbxloop secrets rotate           # replace the agent credential's registration
                                 # (COPILOT_GITHUB_TOKEN, or ANTHROPIC_API_KEY under claude)
                                 # (token from env/.env or --prompt, never argv)
```

`rotate` also reports which secret strategy (proxy vs plain-env fallback) the
next run will use. None of these commands touch the built-in `github` service
secret or registrations owned by other tools.

## Configuration

Configuration resolves, in order, from `SBXLOOP_*` environment variables,
`./sbxloop.toml`, `./pyproject.toml [tool.sbxloop]`, and the host config
`~/.sbxloop/config/sbxloop.toml` (the home's; `sbxloop init` writes it from
the committed `sbxloop.toml.example`) for everything that follows the host
rather than a checkout — which, for the daemon, is all of it. The two
project files are looked for in the current directory and, inside a git
checkout, in each parent up to the checkout's top level — the nearest one
wins, so a command typed from `packages/foo/` of a monorepo sees the root
config. `sbxloop init --project` writes a repository's own starter file;
`sbxloop config show` prints every resolved value, where it came from and
whether a change applies live (the model keys, re-read before every phase)
or at the daemon's next start; `sbxloop config describe KEY` is one key's
card, and `sbxloop config set KEY VALUE` / `unset KEY` change the home's
`config/sbxloop.toml` one key at a time with every comment kept — the same
editor the console's Config screen uses, so the loader judges the whole
file before it is written and the previous file stays beside it as a
timestamped backup. Where things land on disk is not a setting: the home holds it all, and
`SBXLOOP_HOME` moves the home (a `state_dir` key from an older file is
refused by name).

**Whose file is it.** A config file the target repository *carries* —
tracked in git, so any merged pull request can change it, the loop's own
included — is project config: it may set how the tree is built and checked
(`[sandbox] languages`, `gate_command`), how its branches and PRs are named
(`[github] branch_prefix`, `pr_title_template`, `commit_message_template`)
and `[artifacts] exclude`, and nothing else. Egress policy, the merge gate,
budgets, the daemon, which repository the token delivers to: those are
honoured only from files the operator owns — the home's config, an
*untracked* `sbxloop.toml`, or the environment. Keys a tracked file may not
set are dropped with a `config.project_layer.ignored` warning naming them.
Secrets have one place, the home's `config/secrets.env`; a checkout's
`.env` belongs to the application in it and is never read.

The notable knobs:

| Key                                                                                                | Default                                                                                         | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| -------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `[agent] backend`                                                                                  | `copilot`                                                                                       | Agent SDK: `copilot`, `claude`, `codex` or `openai`. Codex uses `OPENAI_API_KEY` and the worker codex extra; `openai` runs the `openai` client against the endpoint `[agent.openai]` names; see [Agent backends](#agent-backends-copilot-claude-codex-or-an-openai-compatible-endpoint).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `model`                                                                                            | `auto`                                                                                          | Default model id for the single `[agent] backend`; roles inherit it when unset. `--model` forces all run agents and survives resume; concierge is unchanged. See [Agent models](#agent-models).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `[agent.models] decompose` / `build` / `review` / `steer` / `reauthor_verify`                      | unset                                                                                           | Independent code-agent model choices; inherit top-level `model`. Reread before each new phase.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `[agent.models] operator_plan` / `operator_execute` / `operator_judge`                             | unset                                                                                           | Independent workload-agent model choices, with the same inheritance and live refresh.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `[github.repos.agent_models] <phase>`                                                              | unset                                                                                           | Sparse per-repository overrides for any of the eight role keys. Unset roles inherit global `[agent.models]`, then `model`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `[agent.openai] base_url`                                                                          | unset                                                                                           | The OpenAI-compatible root the `openai` backend's model calls go to, version path included (`http://vllm.internal:8000/v1`). Required under `backend = "openai"`, parsed at config load; a hostname on your network, an IPv4/IPv6 literal or an explicit port are accepted here only — a plan still declares egress as dotted domains. The host joins the agent sandbox's allowlist and is what the credential is bound to.                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `[agent.openai] api_key_env`                                                                       | `OPENAI_API_KEY`                                                                                | The env var **name** holding the endpoint's credential, never the key; registered with sbx bound to the endpoint's host. An endpoint wanting no credential still needs a placeholder value in it. Not also settable through `[sandbox] env`, a registry `auth_env` or `[[credentials]]`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `[agent.openai] request_timeout_s` / `max_retries`                                                 | `600` / `2`                                                                                     | Client patience for one request and how many retries a failed one gets, passed to the SDK client — sized for a local box that may generate slowly.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `[agent.openai] allow_insecure_endpoint`                                                           | `false`                                                                                         | A plain `http://` `base_url` refuses to load unless this is set: the credential would travel in cleartext.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `[github.repos.openai] base_url` / `request_timeout_s` / `max_retries` / `allow_insecure_endpoint` | unset                                                                                           | Sparse per-repository overrides of `[agent.openai]` for a repository whose code must stay on a private endpoint; unset keys inherit. The endpoint only — `api_key_env` is one per host.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `keep_sandboxes` / `keep_on_failure`                                                               | `false`                                                                                         | Sandbox retention for debugging (see above).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `secret_strategy`                                                                                  | `proxy`                                                                                         | `proxy` keeps token values out of the VM; `plain-env` skips the sbx proxy — tokens are piped per job over worker stdin when this sbx supports it, else written to an in-VM env file. On current sbx the cached exec-visibility verdict makes the non-proxy / env-file fallback the common case even under `proxy`, not an edge case (#46; interim hardening #592).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `[sandbox] cpus`                                                                                   | `6`                                                                                             | Positive integer CPUs for each run agent and bake VM; per-repository cpus override. See Sandbox CPU and memory for cutover and recreation.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `[sandbox] memory`                                                                                 | `12g`                                                                                           | Positive whole MiB/GiB cap for each run agent and bake VM; per-repository memory override. See Sandbox CPU and memory for cutover and recreation.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `[sandbox] concierge_cpus`                                                                         | `2`                                                                                             | Positive integer CPUs for the long-lived concierge VM. See Sandbox CPU and memory for cutover and recreation.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `[sandbox] concierge_memory`                                                                       | `4g`                                                                                            | Memory cap for the concierge VM. See Sandbox CPU and memory for cutover and recreation.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `[sandbox] github_cpus`                                                                            | `1`                                                                                             | Positive integer CPUs for each run or daemon GitHub helper. See Sandbox CPU and memory for cutover and recreation.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `[sandbox] github_memory`                                                                          | `2g`                                                                                            | Memory cap for each GitHub helper. See Sandbox CPU and memory for cutover and recreation.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `[sandbox] service_cpus`                                                                           | `1`                                                                                             | Positive integer CPUs for each service helper and diagnostic scratch VM. See Sandbox CPU and memory for cutover and recreation.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `[sandbox] service_memory`                                                                         | `2g`                                                                                            | Memory cap for each service or diagnostic helper. See Sandbox CPU and memory for cutover and recreation.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `[[github.repos]] cpus / memory`                                                                   | `inherit [sandbox]`                                                                             | Sparse overrides for the run agent only; helpers and concierge retain their own allocations. See Sandbox CPU and memory for cutover and recreation.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `[sandbox] template`                                                                               | unset                                                                                           | Baked template ref from `sbxloop bake`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `[sandbox] workspace`                                                                              | unset                                                                                           | The checkout runs are cut from. Unset: the daemon clones each repository into `~/.sbxloop/workspaces/<owner>/<name>` on first use and refreshes it before every run; set it to use a checkout of your own.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `[sandbox] workspace_isolation`                                                                    | `auto`                                                                                          | Per-run clone isolation when `workspace` is a git checkout (see below).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `[sandbox] gate_command`                                                                           | detected                                                                                        | The project's own gate, run over the whole tree before delivery.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `[sandbox] clone_filter`                                                                           | unset                                                                                           | Git partial-clone filter (`"blob:none"`) for the remote clone of a repository with no host checkout; opt-in, see the clone section for the lazy-fetch hazard.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `[sandbox] clone_submodules`                                                                       | `true`                                                                                          | Whether a fresh run clone's submodules are populated — from the host checkout's copy when it has the recorded commit, else from the `.gitmodules` URL with the run's credential; a submodule neither can populate fails provisioning by name. `false` leaves them empty.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `[sandbox] clone_lfs`                                                                              | `true`                                                                                          | Whether a fresh run clone's Git LFS pointer files are populated — from the host checkout's LFS store when it holds the object, else from the repository's LFS endpoint with the run's credential; needs `git-lfs` on the host, and an object neither can supply fails provisioning by name. `false` leaves the pointer files.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `[sandbox] fetch_tags`                                                                             | `"auto"`                                                                                        | Whether a fresh run clone fetches the repository's tags — from the host checkout when it has them, else from origin with the run's credential. `auto` fetches when a manifest names a tag-derived versioning tool (`setuptools_scm`, `hatch-vcs`, `GitVersion`, `git describe`, …); `always` fetches regardless; `never` leaves the `--no-tags` clone as it is.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `[sandbox] extra_allow_domains`                                                                    | `[]`                                                                                            | Static egress allows applied to every run.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `[sandbox] env`                                                                                    | `{}`                                                                                            | Environment for the agent sandbox's worker and everything it runs: plain values only — the agent sandbox holds no operator secret (a registry token goes on `[[registries]] auth_env`, a service key on `[[credentials]]`; `secret_env` is refused by name, #766). Per-repo overridable; see "Environment for the agent sandbox".                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `[sandbox] apt_packages` / `setup_commands`                                                        | `[]` / `[]`                                                                                     | OS packages ensured beside the toolchains (fail closed), and commands run in the workspace before the first phase, each a `sandbox.setup` event. Per-repo overridable; see "OS packages and setup commands".                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `[sandbox] verify_mode`                                                                            | `full`                                                                                          | What the verify phase and the gate decide: `full` gates, `advisory` runs and reports without blocking, `ci-only` skips them and relies on the PR's checks. Per-repo overridable; see "Suites that need services".                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `[[registries]]`                                                                                   | none                                                                                            | Private package registries: an open entry opens `host` for the agent sandbox and writes the ecosystem's client config there (`~/.npmrc`, `PIP_INDEX_URL`, `GOPRIVATE`, `~/.cargo/config.toml`, `settings.xml`, `NuGet.Config`, `BUNDLE_*`); an entry with `auth_env` is reached only from the service sandbox, which downloads data through fixed operations; the host transfers artifacts to the agent, where dependency resolution and offline cache preparation run through `fetch_dependencies`. Per-repo overridable; see "Private package registries".                                                                                                                                                                                                                                                                                           |
| `[[credentials]]`                                                                                  | none                                                                                            | The catalogue of credentials a run may be granted by name (#765): `name`, `env` (the daemon-environment variable holding the value), `host` (the one host it is good for), `header` / `scheme` (how it is attached; `Authorization: Bearer` by default). A granted run gets a third, service sandbox holding exactly those values and a `call_service` build tool; the agent sandbox never holds them. `sbxloop doctor` checks every `env` is set.                                                                                                                                                                                                                                                                                                                                                                                                     |
| `[[mcp]]`                                                                                          | none                                                                                            | External MCP servers the agent sessions may use. `name`, `transport` (`stdio` with a `command` argv, or `http`/`sse` with a `url`), `hosts` (the domains the server contacts, added to the agent allowlist only for credential-free servers — an undeclared host fails at the server's first request), `credential` (a `[[credentials]]` name held only in the service sandbox; requires Streamable HTTP at its HTTPS host, with tools mediated by the host; credentialed stdio and legacy SSE are refused), `roles` (which sessions get it; default builder + operator, never a critic unless you say so). Native servers work with Copilot and Claude; Codex supports credentialed HTTP servers mediated by sbxloop and rejects native MCP specifications. `sbxloop doctor` checks the credentials are set and that every server declares its hosts. |
| `[sandbox] languages`                                                                              | detected                                                                                        | Toolchains pre-installed in the agent sandbox; unset = detect from the workspace's manifests, `python` if none (see below).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `[[workloads]]`                                                                                    | none                                                                                            | Named profiles bounding what a workload run's plan may ask for (#758): `name`, `egress` (host patterns the agent box may be granted; `[policy] deny` still wins), `credentials` (names from `[[credentials]]`, granted on the service sandbox), `sinks` (`chat`, `issue`, `artifact`, `pr`), `repo` (may a plan ask for a checkout in its data directory), `publish` (`auto` publishes once the judge passes; `hold` parks the run until `!sbx release <item>` / `sbxloop resume <run>`), `budgets` (per-key overrides of `[budgets]`), `description`, `languages` (the toolchains the workload's agent box is provisioned with — none by default, since a workload box gets only the baseline tools and the agent backend's runtime, never `[sandbox] languages`). A need outside the profile fails the run closed naming the key here.               |
| `[workload] default`                                                                               | unset                                                                                           | The profile a workload run gets when `--profile` does not name one. Unset: the run has no profile and every declared need is refused. Must name a `[[workloads]]` entry.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `[[schedules]]`                                                                                    | none                                                                                            | **Legacy** (#818): schedules live in the daemon's database — create them from chat (`create_schedule`) or `sbxloop daemon ctl schedules add …`. An entry here (`name`, `profile`, `ask`, one of `every` / `cron`, `timezone`) is imported into the database once on daemon start and ignored after that; doctor asks for it to be removed.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `[workload] result_label`                                                                          | `sbxloop:result`                                                                                | The label a workload's result issue carries (the `issue` sink, #759); ensured on the repository before filing.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `[concierge] edit_config`                                                                          | `true`                                                                                          | Whether the concierge offers the configuration tools: `config_keys` (read any key of the home's `config/sbxloop.toml` as it is on disk) and `set_config` (change one key on your explicit yes and restart the daemon to apply it). False removes both. The chat sections, this gate and `config_locked` are never changed from chat whatever this says.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `[concierge] config_locked`                                                                        | `["policy", "mcp", "credentials", "registries", "github.repos.token_env", "telemetry.dsn_env"]` | Dotted prefixes chat may read but not change — egress, tool grants and credential names by default, because widening those from a chat mention is a different weight of act than raising a cap. A prefix covers everything under it; `*` matches one segment; indices are not segments. Remove one here, on the host, to allow it from chat.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `[entrygraph] enabled`                                                                             | `true`                                                                                          | Whether the concierge offers `start_entrygraph`, the fixed repository-analysis tool run. False removes the tool from its roster entirely.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `[entrygraph] allow_public_urls`                                                                   | `true`                                                                                          | Whether an ask may name an arbitrary public HTTPS repository. False narrows the recipe to the configured repositories; any other target is refused.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `[entrygraph] extra_hosts`                                                                         | `[]`                                                                                            | Extra egress patterns for the scan runtime, on top of the package index. The runtime installs wheels only, so it reaches no code host by default; name what a source build needs where no wheel applies. `[policy] deny` still wins.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `[policy] allow` / `deny`                                                                          | `[]`                                                                                            | Bounds for task-declared egress.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `[github] repo`                                                                                    | unset                                                                                           | The GitHub integration gate: with a repository every run delivers, reviews and merges. `deliver_base`, `create_repo`, `create_public`, `pr_title_template`, `commit_message_template`, `branch_prefix`, `bot_login` beside it.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `[github] api_url`                                                                                 | api.github.com                                                                                  | The GitHub REST root — GitHub Enterprise Server: `https://ghe.example.com/api/v3`. One source of truth for the REST transport, App auth, `gh` (`GH_HOST`) and both sandboxes' network allows; a `GH_HOST` in the daemon's environment that names another host is refused at config load. FIELD-UNVERIFIED on GHES.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `[vcs] kind`                                                                                       | `github`                                                                                        | The version-control backend the repositories live on (#1009): `github`, `gitlab` or `gitea`. `gitlab` answers every role (a merge train only where the project has one; a delivery is one commits-API changeset, and a fix round rewrites the branch rather than deleting it, see Delivery); `gitea` loads and `sbxloop doctor` reports it as not yet implemented. A `gitlab` or `gitea` configuration must also set `[vcs] api_url`. Anything else is refused at load, naming the three. A `[[github.repos]]` entry may set its own `kind`. Locked from the concierge by default.                                                                                                                                                                                                                                                                     |
| `[vcs] api_url`                                                                                    | unset                                                                                           | The forge's API root. For `github` it is the same setting as `[github] api_url`: a `[vcs]` value fills it, and two different values are refused at load.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `[vcs] token_env`                                                                                  | per kind                                                                                        | The host variable the forge token is read from, by name; the value lives in `secrets.env`. Unset: `GITLAB_TOKEN` for `gitlab`, `GITEA_TOKEN` for `gitea`. For `github` the credential is `GH_TOKEN`/`GITHUB_TOKEN` or the App installation, and a `[[github.repos]] token_env` still wins. The doctor's repository row says which variable and whether it is set.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `[[github.repos]] kind`                                                                            | unset                                                                                           | The forge this one repository lives on; unset uses `[vcs] kind`. A run's own github box follows it; the daemon's shared polling box follows `[vcs] kind`, so every repository one daemon polls must share the daemon's kind today.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `[landing]`                                                                                        | see above                                                                                       | `deliver_draft`, `max_review_rounds`, `max_ci_rounds`, `retry_rounds`, `followups`, `followup_label`, `max_followups_per_run`, `ci_poll_interval_s`, `ci_settle_s`, `ci_timeout_s`, `merge_method`, `delete_branch_on_merge`, `merge_update_attempts`, `required_checks`, `ignore_checks`, `ignore_reviewers`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `[artifacts] exclude`                                                                              | see above                                                                                       | Path components dropped from listings, harvest and delivery (replaces the default, does not add to it).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `[budgets]`                                                                                        | see above                                                                                       | `max_revisions_per_task`, `max_replans_per_task`, `max_verify_reauthors_per_task`, `max_tasks`, `max_wall_clock_s`, `per_job_timeout_s`, `max_tool_calls_per_phase`, `max_parallel_tasks`, `repo_context_max_chars`, `outcome_max_chars`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `[limits]`                                                                                         | `85` / `95` / `90`                                                                              | `disk_warn`, `disk_abort`, `mem_warn` percentages (0 disables).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `[daemon] trigger_label` … `workload_label`                                                        | `sbxloop:run` …                                                                                 | The issue labels: `trigger_label`, `in_progress_label`, `completed_label`, `failed_label`, `blocked_label`, `gated_label`, `workload_label` (queues the issue as a workload); each can be renamed per `[[github.repos]]` entry. `sbxloop init-repo` creates them.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `[daemon] max_runs_per_day`                                                                        | `12`                                                                                            | Runs allowed per calendar day, counted by start time in `run_cap_timezone`; the count resets at 00:00 there.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `[daemon] run_cap_timezone`                                                                        | `UTC`                                                                                           | IANA timezone defining the run cap's day boundary.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `[daemon] max_attempts_per_item` / `max_resumes_per_item`                                          | `2` / `2`                                                                                       | Per-item retry and resume caps; `retry_backoff_s`, `max_consecutive_failures`, `breaker_cooldown_s` beside them.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `[daemon] run_stale_after_s`                                                                       | `21600`                                                                                         | With no run executing, non-terminal runs idle this long are reconciled to a terminal state (`0` disables).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `[daemon] supervised`                                                                              | `false`                                                                                         | `restart` (ctl, `!sbx`, the concierge) exits and relies on a service manager to start the daemon again. Under systemd the daemon can tell on its own; set `true` under any other supervisor (launchd, a container runtime, a process manager). With neither, `restart` is refused by name rather than exiting into nothing.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `[daemon] prune_runs_after_days`                                                                   | `14`                                                                                            | Run-directory retention, swept on start and daily (`0` disables).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `[daemon] backups_keep`                                                                            | `10`                                                                                            | Snapshots kept under `~/.sbxloop/backups/` by the daily sweep (`0` keeps all).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `[daemon] workspace_isolation`                                                                     | `clone`                                                                                         | Isolation for daemon runs against a git-checkout workspace (dirty tree proceeds with a warning).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `[daemon] refresh_workspace`                                                                       | `true`                                                                                          | `git fetch` + fast-forward the workspace checkout before each fresh daemon run.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `[tui] operator_id` / `emoji` / `daemon_unit` / `refresh_s` / `retention_days`                     | `""` / `true` / `sbxloop-daemon` / `0.5` / `14`                                                 | The operator console (`sbxloop tui`), always on: who it speaks as (empty = the login name), glyph markers, the systemd user unit it tails and restarts, its live refresh interval, and how long the daemon keeps the console's mailbox rows (`0` keeps them). The rendering knobs are the `[discord]` / `[slack]` / `[mattermost]` ones.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `[api] enabled`                                                                                    | `false`                                                                                         | Serve the remote operations API from `sbxloop daemon`: REST under `/v1` with generated OpenAPI at `/v1/openapi.json`, liveness at `/health/live`, readiness at `/health/ready`. Needs the `sbxloop[api]` extra; the daemon refuses to start without it. Off, nothing changes.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `[api] bind` / `port`                                                                              | `127.0.0.1` / `8420`                                                                            | Where the listener binds. Loopback by default: reach it remotely through your reverse proxy, which terminates TLS. Local binding never stands in for identity — every request carries a token.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `[api] trusted_proxies`                                                                            | `[]`                                                                                            | Proxy addresses or CIDRs whose `X-Forwarded-*` headers are believed for the client address the auth limiter keys on. Empty: none are.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `[api] access_token_ttl_s` / `refresh_token_ttl_s`                                                 | `900` / `604800`                                                                                | How long an access token (an Ed25519-signed JWT) and a refresh token live. A refresh token is single-use: presenting one twice revokes its whole family and the client authenticates with its secret again. Tokens are minted at `POST /v1/auth/token` from credentials created with `sbxloop api client create NAME --cap …`; `sbxloop api key rotate` replaces the signing key, keeping the old one until its tokens expire.                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `[api] cors_origins`                                                                               | `[]`                                                                                            | Browser origins allowed to call the API. Empty disables CORS; `*` is refused.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `[api] max_body_bytes` / `max_stream_clients`                                                      | `262144` / `32`                                                                                 | Request bodies above the limit are refused (413); live streams beyond the count are refused (503).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `[api] replay_retention_s` / `idempotency_retention_s` / `operation_deadline_s`                    | `604800` / `86400` / `300`                                                                      | How long the public chronology is kept for replay (a client resuming from a pruned cursor is told, 410), how long an `Idempotency-Key` returns the same operation, and how long an accepted command may go unclaimed before it expires rather than applying stale intent.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |

The `[sandbox]` resource settings size each VM through `sbx create` CPU and
memory flags; see [Sandbox CPU and memory](#sandbox-cpu-and-memory).
Pressure inside that allocation is visible through `[limits]`: `mem_warn` emits
a warning; `mem_abort` (off by default, because a parallel test run spikes
memory transiently) fails the task with an explicit "sandbox memory
exhausted" error instead of letting an in-VM OOM surface as an inexplicable
test failure.

### Sizing budgets for a larger repository

The `[budgets]` defaults (2 h wall clock, 60 tool calls per phase) suit a
project whose gate finishes in seconds. The signal that they are too small
is measured gate duration, not repository size: when one run of the gate
command — the test suite plus linters, what every verify pass executes —
takes two minutes or more, 20 tasks × 3 attempts × verify presses on the wall
clock and a multi-package tree eats the tool cap before any edit. `sbxloop init --preset large-repo` writes a starter file with the packaged preset
appended (4 h wall clock, tool cap 80, `[limits]` with `mem_abort` on); the
preset ships inside the wheel as
[`sbxloop/data/presets/large-repo.toml`](../packages/sbxloop/src/sbxloop/data/presets/large-repo.toml)
and its header says how to apply the same sections to an existing file.
Verify output handed back to the builder keeps the first 2 KB and the last
4 KB of each command, so a long test run's first traceback and its failure
summary both survive.
