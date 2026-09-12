"""The agent backend descriptor (#617).

``[agent] backend`` picks which SDK the agent sandbox runs — GitHub Copilot
(the default), Claude or Codex — and everything on the host that is *about* that
choice reads it from here: which env var carries the credential and which
host sbx binds it to, which hosts the credential path must reach, what the
credential is called when it is missing, and where its model ids come from.
Doctor, ``sbxloop secrets``, ``sbxloop list-models``, provisioning and
sandbox pruning all consult :func:`backend_for` instead of assuming Copilot,
so a claude-backend host is diagnosed, rotated and listed as itself.

The Copilot descriptor carries the exact strings those commands printed
before the descriptor existed — a copilot deployment reads byte-identical.

A backend's credential path — the env var, the host it is bound to, the
hosts it must reach, the wording when it is missing — is read through
accessors that take the loaded :class:`~sbxloop.config.Config`, never off a
constant. The three SDK-vendor backends bind to a fixed host and answer
every config the same way; the accessors exist so a backend whose endpoint
the operator names can answer from config instead, and so no consumer can
ask for a host before one is knowable.

This module imports nothing from the config package at runtime (only the
type), so the low-level modules that need the credential constants —
``sbx.secretstate`` re-exports them — never form an import cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sbxloop.config import Config

COPILOT_TOKEN_ENV = "COPILOT_GITHUB_TOKEN"  # nosec B105 - env var name, not a secret

# The PAT is exchanged for a Copilot API token at api.github.com; the
# exchanged token lives in SDK process memory, so the copilot API hosts only
# need network allows - never an env rewrite. One env var also cannot be
# registered twice: sbx keys custom secrets by env name, so binding the same
# env to two hosts fails with "already exists".
#
# Deliberately NOT derived from `[github] api_url` (#623): Copilot is a
# github.com service even for Enterprise Server customers (licensed and
# served through GitHub Connect), so the credential it exchanges is a
# github.com credential whichever host the repository lives on.
# FIELD-UNVERIFIED on GHES — recorded as the known unknown it is.
COPILOT_TOKEN_HOST = "api.github.com"  # nosec B105 - hostname, not a secret

# The claude agent backend's credential (#533): an Anthropic API key, sent
# directly to the API host by the Claude Code CLI the Claude Agent SDK
# spawns.
ANTHROPIC_TOKEN_ENV = "ANTHROPIC_API_KEY"  # nosec B105 - env var name, not a secret
ANTHROPIC_TOKEN_HOST = "api.anthropic.com"  # nosec B105 - hostname, not a secret

# The Codex backend authenticates the bundled app server with this API key
# over its local JSON-RPC transport. No account login or host auth store is
# imported into the sandbox.
OPENAI_TOKEN_ENV = "OPENAI_API_KEY"  # nosec B105 - env var name, not a secret
OPENAI_TOKEN_HOST = "api.openai.com"  # nosec B105 - hostname, not a secret


@dataclass(frozen=True)
class CredentialBinding:
    """One backend's credential path as it stands under one config.

    ``token_env``/``token_host`` are the sbx custom-secret registration
    provisioning makes for the agent sandbox; ``token_hosts`` are the
    network hosts that credential path has to reach (doctor checks the
    policy for each). ``missing_token_detail`` is doctor's row text and
    ``missing_token_error`` provisioning's failure — kept as literal
    strings per backend rather than templated so the copilot wording never
    drifts.
    """

    token_env: str
    token_host: str
    token_hosts: tuple[str, ...]
    missing_token_detail: str
    missing_token_error: str

    @property
    def secret(self) -> tuple[str, str]:
        """The ``(env, host)`` custom-secret registration this binding owns."""
        return (self.token_env, self.token_host)


#: How a backend binds its credential under a config: a constant for a
#: backend whose host is fixed, a resolver for one that reads it from config.
Binding = CredentialBinding | Callable[["Config"], CredentialBinding]


@dataclass(frozen=True)
class AgentBackend:
    """One agent backend as the host sees it.

    Everything about the credential path goes through :meth:`bound` — the
    config-taking accessors below are the only way to read it, so a caller
    without a loaded config cannot name a host that may not be knowable
    without one.
    """

    name: str
    label: str
    credential: str
    create_url: str
    models_source: str
    binding: Binding

    @property
    def is_default(self) -> bool:
        return self.name == "copilot"

    def bound(self, config: Config) -> CredentialBinding:
        """The credential path under ``config``."""
        if isinstance(self.binding, CredentialBinding):
            return self.binding
        return self.binding(config)

    def token_env(self, config: Config) -> str:
        """The env var carrying the agent sandbox's credential."""
        return self.bound(config).token_env

    def token_host(self, config: Config) -> str:
        """The host sbx binds the credential to."""
        return self.bound(config).token_host

    def token_hosts(self, config: Config) -> tuple[str, ...]:
        """The hosts the credential path must reach."""
        return self.bound(config).token_hosts

    def secret(self, config: Config) -> tuple[str, str]:
        """The ``(env, host)`` custom-secret registration this backend owns."""
        return self.bound(config).secret

    def missing_token_detail(self, config: Config) -> str:
        """Doctor's row text when the credential is not set."""
        return self.bound(config).missing_token_detail

    def missing_token_error(self, config: Config) -> str:
        """Provisioning's failure when the credential is not set."""
        return self.bound(config).missing_token_error

    def doctor_check_name(self, config: Config) -> str:
        """The credential row's name: bare for the default backend, tagged
        with the backend otherwise so a reader sees *why* it is the row."""
        token_env = self.token_env(config)
        if self.is_default:
            return token_env
        return f"{token_env} (agent backend: {self.name})"

    def has_token(self, config: Config, env: dict[str, str]) -> bool:
        return bool(env.get(self.token_env(config)))


COPILOT_BINDING = CredentialBinding(
    token_env=COPILOT_TOKEN_ENV,
    token_host=COPILOT_TOKEN_HOST,
    token_hosts=("api.githubcopilot.com", "api.github.com"),
    missing_token_detail=(
        'not set — create a fine-grained PAT with the "Copilot Requests" '
        f"permission and export {COPILOT_TOKEN_ENV}"
    ),
    missing_token_error=(
        f"{COPILOT_TOKEN_ENV} is not set on the host. Create a fine-grained PAT "
        'with the "Copilot Requests" permission and export it.'
    ),
)

COPILOT = AgentBackend(
    name="copilot",
    label="copilot",
    credential='a fine-grained PAT with the "Copilot Requests" permission',
    create_url="https://github.com/settings/personal-access-tokens",
    models_source="the SDK",
    binding=COPILOT_BINDING,
)

CLAUDE_BINDING = CredentialBinding(
    token_env=ANTHROPIC_TOKEN_ENV,
    token_host=ANTHROPIC_TOKEN_HOST,
    token_hosts=(ANTHROPIC_TOKEN_HOST,),
    missing_token_detail=(
        "not set — create an Anthropic API key and export "
        f'{ANTHROPIC_TOKEN_ENV}, or switch [agent] backend back to "copilot"'
    ),
    missing_token_error=(
        f'{ANTHROPIC_TOKEN_ENV} is not set on the host but [agent] backend = "claude". '
        "Create an Anthropic API key and export it, or switch back to "
        'backend = "copilot".'
    ),
)

CLAUDE = AgentBackend(
    name="claude",
    label="claude",
    credential="an Anthropic API key",
    create_url="https://console.anthropic.com/settings/keys",
    models_source="the Anthropic Models API",
    binding=CLAUDE_BINDING,
)

CODEX_BINDING = CredentialBinding(
    token_env=OPENAI_TOKEN_ENV,
    token_host=OPENAI_TOKEN_HOST,
    token_hosts=(OPENAI_TOKEN_HOST,),
    missing_token_detail=(
        "not set — create an OpenAI API key and export "
        f'{OPENAI_TOKEN_ENV}, or switch [agent] backend back to "copilot"'
    ),
    missing_token_error=(
        f'{OPENAI_TOKEN_ENV} is not set on the host but [agent] backend = "codex". '
        "Create an OpenAI API key and export it, or switch back to "
        'backend = "copilot".'
    ),
)

CODEX = AgentBackend(
    name="codex",
    label="codex",
    credential="an OpenAI API key",
    create_url="https://platform.openai.com/api-keys",
    models_source="the Codex SDK",
    binding=CODEX_BINDING,
)

#: Every backend ``[agent] backend`` accepts, default first. The config
#: Literal and ``daemon.discord_format.KNOWN_BACKENDS`` name the same set.
BACKENDS: tuple[AgentBackend, ...] = (COPILOT, CLAUDE, CODEX)

_BY_NAME = {backend.name: backend for backend in BACKENDS}


def backend_named(name: str) -> AgentBackend:
    """The descriptor for ``name``; an unknown name is a programming error
    (config validation already limits the Literal), reported as such."""
    try:
        return _BY_NAME[name]
    except KeyError:
        known = ", ".join(b.name for b in BACKENDS)
        raise ValueError(f"unknown agent backend {name!r} (known: {known})") from None


def backend_for(config: Config) -> AgentBackend:
    """The descriptor ``[agent] backend`` selects."""
    return backend_named(config.agent.backend)
