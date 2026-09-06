# sbxloop

<p>
  <a href="https://github.com/brettbergin/sbxloop/actions/workflows/ci.yml"><img src="https://github.com/brettbergin/sbxloop/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://pypi.org/project/sbxloop/"><img src="https://img.shields.io/pypi/v/sbxloop" alt="PyPI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT" /></a>
</p>

<p>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.13%2B-3776AB?logo=python&amp;logoColor=white" alt="Python 3.13+" /></a>
  <a href="https://docs.docker.com/ai/sandboxes/"><img src="https://img.shields.io/badge/Docker-Sandboxes-2496ED?logo=docker&amp;logoColor=white" alt="Docker Sandboxes" /></a>
  <a href="https://www.sqlite.org/"><img src="https://img.shields.io/badge/SQLite-003B57?logo=sqlite&amp;logoColor=white" alt="SQLite" /></a>
  <a href="https://docs.pydantic.dev/"><img src="https://img.shields.io/badge/Pydantic-E92063?logo=pydantic&amp;logoColor=white" alt="Pydantic" /></a>
  <a href="https://typer.tiangolo.com/"><img src="https://img.shields.io/badge/Typer-009688" alt="Typer" /></a>
  <a href="https://rich.readthedocs.io/"><img src="https://img.shields.io/badge/Rich-4051B5" alt="Rich" /></a>
  <a href="https://textual.textualize.io/"><img src="https://img.shields.io/badge/Textual-181717" alt="Textual" /></a>
  <a href="https://www.structlog.org/"><img src="https://img.shields.io/badge/structlog-555555" alt="structlog" /></a>
</p>

<p>
  <a href="https://github.com/github/copilot-sdk"><img src="https://img.shields.io/badge/GitHub_Copilot-SDK-000000?logo=githubcopilot&amp;logoColor=white" alt="GitHub Copilot SDK" /></a>
  <a href="https://github.com/anthropics/claude-agent-sdk-python"><img src="https://img.shields.io/badge/Claude-Agent_SDK-D97757?logo=claude&amp;logoColor=white" alt="Claude Agent SDK" /></a>
  <a href="https://discordpy.readthedocs.io/"><img src="https://img.shields.io/badge/Discord-5865F2?logo=discord&amp;logoColor=white" alt="Discord" /></a>
  <a href="https://github.com/slackapi/python-slack-sdk"><img src="https://img.shields.io/badge/Slack-4A154B" alt="Slack" /></a>
  <a href="https://docs.astral.sh/uv/"><img src="https://img.shields.io/badge/uv-DE5FE9?logo=uv&amp;logoColor=white" alt="uv" /></a>
  <a href=".github/workflows/ci.yml"><img src="https://img.shields.io/badge/GitHub_Actions-2088FF?logo=githubactions&amp;logoColor=white" alt="GitHub Actions" /></a>
</p>

**Give it the work. Keep the steering wheel.**

Built on [Docker Sandboxes (`sbx`)](https://docs.docker.com/ai/sandboxes/),
sbxloop takes an ask from chat, a labeled issue, or your terminal and works
it through to a merged pull request. It plans the change, writes the code,
runs checks, handles review feedback, and follows CI through to the finish.
You can watch, steer, or stop it along the way.

Docker's `sbx` CLI provides the isolated sandbox runtime. sbxloop provisions
the sandboxes, coordinates the agents and GitHub operations, and cleans up
when the run ends.

It's built for the work you want off your plate without spending the afternoon
relaying messages between an agent, your terminal, and a pull request. Your
repository's checks and review requirements still apply, and retry, time,
and spending budgets put a limit on how long the agent can keep trying.

## Why the secrets live elsewhere

A useful coding agent needs a shell. It reads unfamiliar files, runs build
scripts, and takes direction from issues and review comments. Giving that
same environment a repository token means a malicious instruction or script
could turn a coding task into a stolen credential. A sandbox limits where
code runs; a secret inside it is still a secret that code can read.

**sbxloop keeps the GitHub token out of the coding agent's sandbox.** The
agent edits files in one Docker Sandbox. A separate sandbox holds the token
and performs a fixed set of GitHub operations, with no model running there.
The host moves the changes and mediates requests between them; the agent
cannot connect directly to the credential sandbox or run arbitrary commands
inside it. Service credentials follow the same pattern when needed.

The agent keeps its own model credential, but it doesn't need your repository
or service keys to do its work. Its proposed changes still go through checks
and review before they land. That's the point of the split: enough freedom
to get the job done, without putting every key on the workbench.
The [security architecture](docs/architecture.md#the-credential-split-in-one-picture)
explains the boundaries and how credentials are handled.

Need a report or a set of files instead? [Workload runs](docs/architecture.md#workloads)
use the same supervised loop and publish the result without a code merge.

## Get started

You'll need a host that supports [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/)
and either GitHub Copilot access or an Anthropic API key. sbxloop requires
Python 3.13 or newer; the installer sets up Python and the sandbox CLI for you.

### Install and initialize

On macOS or Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/brettbergin/sbxloop/main/scripts/install.sh | sh
export PATH="$HOME/.sbxloop/bin:$PATH"
```

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

Prefer the terminal? Run `sbxloop tui` on the daemon host. The
[TUI](docs/tui.md) puts the queue, live runs, logs, and chat in one place,
so you can steer work and approve held merges without switching to Discord
or Slack. It uses the same home and configuration as the daemon.

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
