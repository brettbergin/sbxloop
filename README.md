# sbxloop

<p>
  <a href="https://github.com/brettbergin/sbxloop/actions/workflows/ci.yml"><img src="https://github.com/brettbergin/sbxloop/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://pypi.org/project/sbxloop/"><img src="https://img.shields.io/pypi/v/sbxloop" alt="PyPI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT" /></a>
</p>

## Infrastructure & Runtime

<p>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.13%2B-3776AB?logo=python&amp;logoColor=white" alt="Python 3.13+" /></a>
  <a href="https://docs.docker.com/ai/sandboxes/"><img src="https://img.shields.io/badge/Docker-Sandboxes-2496ED?logo=docker&amp;logoColor=white" alt="Docker Sandboxes" /></a>
</p>

## Data & Configuration

<p>
  <a href="https://www.sqlite.org/"><img src="https://img.shields.io/badge/SQLite-003B57?logo=sqlite&amp;logoColor=white" alt="SQLite" /></a>
  <a href="https://docs.pydantic.dev/"><img src="https://img.shields.io/badge/Pydantic-E92063?logo=pydantic&amp;logoColor=white" alt="Pydantic" /></a>
</p>

## CLI & User Interface

<p>
  <a href="https://typer.tiangolo.com/"><img src="https://img.shields.io/badge/Typer-009688" alt="Typer" /></a>
  <a href="https://rich.readthedocs.io/"><img src="https://img.shields.io/badge/Rich-4051B5" alt="Rich" /></a>
  <a href="https://textual.textualize.io/"><img src="https://img.shields.io/badge/Textual-181717" alt="Textual" /></a>
</p>

## Logging & Observability

<p>
  <a href="https://www.structlog.org/"><img src="https://img.shields.io/badge/structlog-555555" alt="structlog" /></a>
</p>

## Agent & Language Model Support

<p>
  <a href="https://github.com/github/copilot-sdk"><img src="https://img.shields.io/badge/GitHub_Copilot-SDK-000000?logo=githubcopilot&amp;logoColor=white" alt="GitHub Copilot SDK" /></a>
  <a href="https://github.com/anthropics/claude-agent-sdk-python"><img src="https://img.shields.io/badge/Claude-Agent_SDK-D97757?logo=claude&amp;logoColor=white" alt="Claude Agent SDK" /></a>
  <a href="https://developers.openai.com/codex/sdk/"><img src="https://img.shields.io/badge/OpenAI_Codex-SDK-000000?logo=openai&amp;logoColor=white" alt="OpenAI Codex SDK" /></a>
  <a href="https://platform.openai.com/docs/api-reference/chat"><img src="https://img.shields.io/badge/OpenAI-compatible_API-412991?logo=openai&amp;logoColor=white" alt="OpenAI-compatible API" /></a>
</p>

## Version Control & Forge Integration

<p>
  <a href="https://github.com/"><img src="https://img.shields.io/badge/GitHub-supported-181717?logo=github&amp;logoColor=white" alt="GitHub supported" /></a>
  <a href="https://about.gitlab.com/"><img src="https://img.shields.io/badge/GitLab-config_preview-FC6D26?logo=gitlab&amp;logoColor=white" alt="GitLab configuration preview" /></a>
  <a href="https://about.gitea.com/"><img src="https://img.shields.io/badge/Gitea-config_preview-609926?logo=gitea&amp;logoColor=white" alt="Gitea configuration preview" /></a>
</p>

## Chat & Notifications

<p>
  <a href="https://discordpy.readthedocs.io/"><img src="https://img.shields.io/badge/Discord-5865F2?logo=discord&amp;logoColor=white" alt="Discord" /></a>
  <a href="https://github.com/slackapi/python-slack-sdk"><img src="https://img.shields.io/badge/Slack-4A154B?logo=slack&amp;logoColor=white" alt="Slack" /></a>
  <a href="https://developers.mattermost.com/integrate/reference/"><img src="https://img.shields.io/badge/Mattermost-0058CC?logo=mattermost&amp;logoColor=white" alt="Mattermost" /></a>
</p>

## Toolchain & Automation

<p>
  <a href="https://docs.astral.sh/uv/"><img src="https://img.shields.io/badge/uv-DE5FE9?logo=uv&amp;logoColor=white" alt="uv" /></a>
  <a href=".github/workflows/ci.yml"><img src="https://img.shields.io/badge/GitHub_Actions-2088FF?logo=githubactions&amp;logoColor=white" alt="GitHub Actions" /></a>
</p>

**Give sbxloop the work and ditch the steering wheel.**

Built on [Docker Sandboxes (`sbx`)](https://docs.docker.com/ai/sandboxes/),
sbxloop automates code changes from chat, issues, or your terminal—planning,
coding, testing, and handling feedback through to merge. For general
[agentic workloads](docs/architecture.md#workloads), it researches, writes
reports, or generates files. Watch, steer, or stop it anytime.

Out-of-scope findings are checked against existing issues; tracked problems
link to their issue, while uncertain findings remain as PR notes.

Docker's `sbx` CLI provides the sandbox runtime. sbxloop provisions sandboxes,
coordinates agents and GitHub operations, and cleans up when done.

Built to free you from relaying messages between agent, terminal, and pull
request. Your repository's checks and review requirements still apply, with
retry, time, and spending budgets limiting the agent's efforts.

## Why the secrets live elsewhere

A useful coding agent needs a shell. It reads unfamiliar files, runs build
scripts, and takes direction from issues and review comments. Giving that
same environment a repository token means a malicious instruction or script
could turn a coding task into a stolen credential. A sandbox limits where
code runs; a secret inside it is still a secret that code can read.

**sbxloop keeps the GitHub token out of the coding agent's sandbox.** The
agent runs in one sandbox and edits files; a separate sandbox holds the token
and performs GitHub operations. The host mediates between them—the agent
cannot access the credential sandbox or run arbitrary commands there.

The agent keeps its own model credential but doesn't need your repository or
service keys. Its proposed changes still go through checks and review before
landing. The [security architecture](docs/architecture.md#the-credential-split-in-one-picture)
explains the boundaries and credential handling.

Need a report or a set of files instead? [Workload runs](docs/architecture.md#workloads)
use the same supervised loop and publish the result without a code merge.

## Get started

You'll need a host that supports [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/)
and GitHub Copilot access, an Anthropic API key, or an OpenAI API key. sbxloop requires
Python 3.13 or newer; the installer sets up Python and the sandbox CLI for you.

The host needs curl, tar, git, and e2fsprogs; the installer checks for git and reports any missing dependencies.

**Installing and preparing the host are two different jobs.** Everything sbxloop installs
lands under `~/.sbxloop`, a directory your account already owns; nothing in the install
needs root. Preparing the host is separate, one-time, and partly an administrator's:

| One-time host preparation (Linux)   | Who                      | Why                                                            |
| ----------------------------------- | ------------------------ | -------------------------------------------------------------- |
| `/dev/kvm` present                  | administrator            | every sandbox is a microVM; without the device none boots      |
| `/dev/kvm` openable by your account | administrator            | Docker's Linux setup adds the account to the `kvm` group       |
| e2fsprogs installed                 | administrator            | the sandbox backend formats its block devices with `mkfs.ext4` |
| sbx's AppArmor profile installed    | administrator            | the last step of Docker's installer writes into `/etc`         |
| `loginctl enable-linger $USER`      | you, or an administrator | only for an unattended daemon: user services stop at logout    |

The installer reports each of these and changes none of them — it joins no group, installs
no package, and elevates nothing. `sbxloop doctor` shows the same rows at any time, and
`sbxloop init` refuses by name the one step that cannot work without them. On macOS none of
them apply: the platform brings its own virtualisation, and an interactive install needs no
service manager at all (`sbxloop init --no-systemd`).

### Install and initialize

On macOS or Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/brettbergin/sbxloop/main/scripts/install.sh | sh
export PATH="$HOME/.sbxloop/bin:$PATH"
```

On Windows, use WSL2: install a Linux distribution, turn on Docker Desktop's
WSL integration for it, and run the same two lines inside that distribution.
Native Windows cannot boot the sandboxes; `sbxloop run`, `daemon` and `bake`
refuse there by name and `sbxloop doctor` says so in its first row. What it
*can* do — lay out a home, find your config and secrets, and diagnose
itself — is spelled out under
[Platform support](docs/user-guide.md#platform-support).

`sbxloop init` creates the home directory, installs the runtime, and writes
your starter configuration and secrets file:

```bash
sbxloop init
```

The installer above already runs this step. If you installed with pip, run
it yourself. You can also rerun it to repair an existing home.
For an older installation, read the [home migration notes](CHANGELOG.md#10-cutover).

### Configure your agent

Everything lives under `~/.sbxloop` by default. Set `SBXLOOP_HOME` before
initializing if you want it somewhere else. The two files you'll edit are:

```text
~/.sbxloop/config/
  sbxloop.toml   # Models, repositories, budgets, and other settings
  secrets.env    # Credentials, kept outside your checkout
```

The generated `sbxloop.toml` includes commented settings and their defaults.
Uncomment the section header and the settings you want to change. For the
default Copilot backend, put a fine-grained token with **Copilot Requests**
permission in `secrets.env`:

```dotenv
COPILOT_GITHUB_TOKEN=your_copilot_token
```

Prefer Claude? Set the agent section in `sbxloop.toml`:

```toml
[agent]
backend = "claude"
```

Then put `ANTHROPIC_API_KEY=your_api_key` in `secrets.env` instead.
You only need the credential for the backend you choose.

For Codex, use `backend = "codex"` and put `OPENAI_API_KEY=your_api_key` in
`secrets.env`. Provisioning installs the Python Codex SDK in the agent sandbox.
For a self-hosted server or gateway speaking the OpenAI wire shape, use
`backend = "openai"` and name the endpoint under `[agent.openai]`.
The [backend guide](docs/user-guide.md#agent-backends-copilot-claude-codex-or-an-openai-compatible-endpoint)
covers setup and model listing; the [Codex implementation plan](docs/codex-backend.md)
and the [OpenAI-compatible endpoint plan](docs/openai-backend.md) record their
tool contracts and verification limits.

Models can differ by agent while sharing one backend: for example, use Haiku
for the concierge, Opus for the builder, and Sonnet for review. Configure
`[agent.models]`, optional per-repository `agent_models`, and `[concierge] model`.
Unset roles inherit top-level `model`; `--model` forces all run agents for that
run. Edits take effect before the next phase, with a fresh session when the
model changes. The TUI offers a searchable model picker backed by a cached
backend catalog; successful provisioning and `list-models` refresh that cache.
See [Agent models](docs/user-guide.md#agent-models).

The home config holds your operator settings. For a project's build and
check settings, `sbxloop init --project` creates a `sbxloop.toml` in the
current directory. Tracked project config cannot change your credentials,
network policy, or merge approvals. Environment overrides take precedence;
`sbxloop config show` prints the resolved settings and where they came from.

### Run your first task

Log in to Docker Sandboxes, initialize its network policy, and check your setup:

```bash
sbx login
sbx policy init balanced
sbxloop doctor
```

From the checkout you want to work on:

```bash
sbxloop run "Add tests for the retry logic and fix any bugs they uncover"
```

A live dashboard shows the work as it happens. Without a configured GitHub
repository, the run finishes with its changes and artifacts available locally.

### Take it through to a pull request

Add a separate repository token to `secrets.env`, following the
[GitHub permissions guide](docs/permissions.md):

```dotenv
GH_TOKEN=your_repository_token
```

Alternatively, use [GitHub App authentication](docs/user-guide.md#github-app-auth)
by setting `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, and
`GITHUB_APP_PRIVATE_KEY_PATH` in `secrets.env`. Leave `GH_TOKEN` and
`GITHUB_TOKEN` unset when using the App.

Name the target repository for one run:

```bash
sbxloop run "Add tests for the retry logic" --repo your-org/your-repo
```

**This can merge the PR automatically** once checks and repository rules allow it.
For approval in chat before merging, configure
[`[landing] merge_gate = "chat"`](docs/user-guide.md#github-integration)
with a chat bridge.

To save the repository for future runs, set it in your home `sbxloop.toml`:

```toml
[github]
repo = "your-org/your-repo"
```

Future runs can omit `--repo`. Leave GitHub unconfigured if you only want
local changes and artifacts.

## Stay in control

You don't have to babysit a run to know what happened. The live dashboard
shows its progress, and the history and artifacts stay available afterward.
If a run stops, a checkpoint gives you somewhere to resume.

| Command                        | Use it to                             |
| ------------------------------ | ------------------------------------- |
| `sbxloop status`               | See what's running and what finished. |
| `sbxloop logs RUN`             | Read a run's full history.            |
| `sbxloop cancel RUN`           | Stop a run.                           |
| `sbxloop resume RUN`           | Continue from a saved checkpoint.     |
| `sbxloop artifacts RUN --tree` | Find the files it produced.           |
| `sbxloop doctor`               | Diagnose setup problems.              |

For ongoing work, run `sbxloop daemon` with configured repositories and a
[Discord or Slack bridge](docs/user-guide.md#the-daemon-an-always-on-outer-loop).
It picks up labeled issues and lets you follow and steer runs from chat.

Working from somewhere else? Switch on the [remote API](docs/api.md): the
same daemon serves REST, server-sent events and a WebSocket, so a client with
a scoped token can admit work, follow a run, steer it, approve a held merge
and fetch its artifacts without a shell on the host.

Prefer the terminal? Run `sbxloop tui` on the daemon host. The
[TUI](docs/tui.md) puts the queue, live runs, logs, and chat in one place,
so you can steer work and approve held merges without switching to Discord
or Slack. It uses the same home and configuration as the daemon — and edits
that configuration in place: pick a setting, change it, and the file is
written back with its comments intact.

## Run as a service on Debian

Once Docker Sandboxes is working on your Debian host and the setup above
passes `sbxloop doctor`, you can leave the daemon running under systemd.
Log in as the account that owns your sbxloop home and create the user services:

```bash
sbxloop init --systemd
```

This writes `sbxloop-daemon.service` and `sbx-sandboxd.service` under
`~/.sbxloop/systemd/` and enables them through `systemctl --user`.
It preserves your configuration and does not start the services yet.
The shell installer already performs this step; rerunning it is safe.

Init also enables [lingering](https://manpages.debian.org/bookworm/systemd/loginctl.1.en.html#User_Commands),
which lets the services start at boot and keep running after logout. If it
reports a permission error for that step, enable it with:

```bash
sudo loginctl enable-linger "$USER"
```

With your repositories and credentials configured in `~/.sbxloop/config/`,
start the daemon and check its logs:

```bash
systemctl --user start sbxloop-daemon
systemctl --user status sbxloop-daemon
journalctl --user -u sbxloop-daemon -f
```

The sandbox backend starts first automatically. Run these `systemctl --user`
commands as the same account you used for setup, without `sudo`.
For upgrades, follow the [deployment guide](docs/deploy.md#upgrading-by-hand)
to let active work finish before restarting.

## Make it yours

When you need to adjust models, budgets, or network access, the
[configuration reference](docs/user-guide.md#configuration) and
[example config](packages/sbxloop/src/sbxloop/data/sbxloop.toml.example)
cover the options.

If your tests need services that only CI provides, see
[service-backed verification](docs/user-guide.md#suites-that-need-services).
For a daemon that stays running, follow the [deployment guide](docs/deploy.md).
The [user guide](docs/user-guide.md) has the full command reference and help
for when something gets stuck.

Optional [GlitchTip error reporting](docs/user-guide.md#glitchtip-error-reporting)
sends host failures to your own project. Set the DSN in the home's secrets file.

| Key                     | Default         | Meaning                                                                            |
| ----------------------- | --------------- | ---------------------------------------------------------------------------------- |
| `telemetry.dsn_env`     | `GLITCHTIP_DSN` | Environment variable holding the reporting DSN; empty or unset disables reporting. |
| `telemetry.environment` | `production`    | Deployment label on error reports.                                                 |
| `telemetry.log_fields`  | `diagnostic`    | How much of a log record a report carries: `none`, `diagnostic`, `all`.            |

## Contributing

<p>
  <a href="https://docs.astral.sh/ruff/"><img src="https://img.shields.io/badge/Ruff-D7FF64?logo=ruff&amp;logoColor=black" alt="Ruff" /></a>
  <a href="https://mypy.readthedocs.io/"><img src="https://img.shields.io/badge/mypy-strict-2A6DB2" alt="mypy" /></a>
  <a href="https://docs.pytest.org/"><img src="https://img.shields.io/badge/pytest-0A9EDC?logo=pytest&amp;logoColor=white" alt="pytest" /></a>
  <a href="https://bandit.readthedocs.io/"><img src="https://img.shields.io/badge/Bandit-security-F5C542" alt="Bandit" /></a>
  <a href="https://github.com/hukkin/mdformat"><img src="https://img.shields.io/badge/mdformat-000000?logo=markdown&amp;logoColor=white" alt="mdformat" /></a>
  <a href="https://hatch.pypa.io/"><img src="https://img.shields.io/badge/Hatch-4051B5" alt="Hatch" /></a>
</p>

Read [`AGENTS.md`](AGENTS.md) for the working agreement and required checks,
then [the architecture](docs/architecture.md) for the map.

```bash
make install
make check
make build
```

The [host orchestrator](packages/sbxloop) and [sandbox worker](packages/sbxloop-worker)
ship together. Local tests use a fake sandbox CLI; you don't need Docker
Sandboxes to contribute. Real sandbox tests run on CI runners.

Keep each PR focused, add regression coverage for behavior changes, and follow
the gate sequence in AGENTS.md. See [RELEASING.md](RELEASING.md) for releases
and [CHANGELOG.md](CHANGELOG.md) for what's changed.

[MIT licensed](LICENSE).
