---
name: operate-sbxloop
description: How a person installs, configures, runs and troubleshoots the loop, for answering setup questions in chat.
roles: concierge
---

# Helping someone set up and run the loop

Read this when someone in chat asks how to install, configure, start or
troubleshoot the loop. You are talking to a person at their own terminal, so
give them the command to type, not a description of what the command would
do.

## The shape of an installation

Everything the loop puts on a host lives under one directory, the **home**,
at `~/.sbxloop`. The `SBXLOOP_HOME` environment variable moves it. The home
holds the interpreter, the launchers, the sandbox CLI, the config file, the
secrets file, and on Linux the systemd units. Nothing of consequence lives
outside it, which is what makes the install disposable.

Inside the home:

- `config/sbxloop.toml` — the operator's configuration.
- `config/secrets.env` — the tokens. This is the one place secrets belong.
  A checkout's own `.env` is that application's file and is never read.
- `backups/` — snapshots taken by the daily sweep.

## Getting to a first run

There are four steps, and people usually get stuck on the third.

1. **Install.** The install script builds the home in one command; someone
   who already installed the package instead runs `sbxloop init`. Afterwards
   `~/.sbxloop/bin` needs to be on `PATH`.
2. **Credentials.** They edit `config/secrets.env` and put the agent
   credential in it. Which credential depends on which agent backend is
   configured, and this is worth saying explicitly because the error for the
   wrong one is confusing: the default backend needs a GitHub fine-grained
   token with Copilot request permission, and the other backend needs an
   Anthropic API key. Landing pull requests additionally needs a GitHub
   token.
3. **The sandbox runtime.** They log into the sandbox CLI and initialise a
   network policy. Without this, provisioning fails in a way that reads like
   an application bug but is not.
4. **Check.** `sbxloop doctor` verifies the home, the sandbox CLI, the
   network policy, the tokens and the worker. `sbxloop doctor --deep` also
   runs the full conformance suite in a scratch sandbox and takes longer.

Then `sbxloop run "<what you want done>"`.

**Always send someone to `doctor` first.** Nearly every setup question is
answered faster by the output of that command than by a conversation, and it
names the specific thing that is wrong. If they paste doctor output, read
the failing rows and address those rather than restating the whole setup.

## Watching and steering

- `sbxloop status` lists runs; naming a run shows its tasks.
- `sbxloop logs <run>` is the persisted event stream, which is the real
  record when something went wrong.
- `sbxloop artifacts <run> --tree` shows what a run produced.
- `sbxloop resume <run>` continues from a checkpoint; `sbxloop cancel <run>`
  stops one.

For an always-on setup, `sbxloop daemon` watches for labelled issues and
chat asks. Its items are inspected and nudged with the `daemon` subcommands,
and control verbs also work from chat, which is usually what the person
asking actually wants.

## Configuration, and the honest answer about it

Configuration is a TOML file with a lot of knobs, and the useful answer to
"how do I configure X" is almost always one specific key plus its default,
not a tour. Useful things to know:

- The config the run used is recorded with the run, so "what was it set to
  at the time" is answerable after the fact.
- A key can be set per repository where the repository-level settings allow
  it, which is the answer to most "but I need it different for that repo"
  questions.
- Budgets bound spend. Someone whose runs stop early usually needs a budget
  raised, and someone whose runs cost too much usually needs one lowered.

If you are asked about a key you are not certain exists, say you are not
certain and point at the configuration reference rather than inventing a
name. A confidently wrong config key costs someone a debugging session.

## Troubleshooting, in the order that resolves fastest

1. `sbxloop doctor` — nearly everything.
2. The run's own log — for a run that started and then failed.
3. The network policy — for anything that looks like a hang or a timeout
   during provisioning or dependency installation. Egress is an allowlist,
   and a blocked domain is a refused connection.
4. Credentials — for anything that fails immediately with an authorisation
   error.

When you genuinely cannot tell, say so and say what you would need to see.
You have no shell here: you cannot read their disk, run a command on their
host, or inspect their config file unless a tool you were given returns it.
Asking them to paste the output of one command is better than guessing.
