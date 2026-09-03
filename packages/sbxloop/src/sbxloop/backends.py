"""The agent backend descriptor (#617).

``[agent] backend`` picks which SDK the agent sandbox runs — GitHub Copilot
(the default) or Claude — and everything on the host that is *about* that
choice reads it from here: which env var carries the credential and which
host sbx binds it to, which hosts the credential path must reach, what the
credential is called when it is missing, and where its model ids come from.
Doctor, ``sbxloop secrets``, ``sbxloop list-models``, provisioning and
sandbox pruning all consult :func:`backend_for` instead of assuming Copilot,
so a claude-backend host is diagnosed, rotated and listed as itself.

The Copilot descriptor carries the exact strings those commands printed
before the descriptor existed — a copilot deployment reads byte-identical.

This module imports nothing from the config package at runtime (only the
type), so the low-level modules that need the credential constants —
``sbx.secretstate`` re-exports them — never form an import cycle.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class AgentBackend:
    """One agent backend as the host sees it.

    ``token_env``/``token_host`` are the sbx custom-secret registration
    provisioning makes for the agent sandbox; ``token_hosts`` are the
    network hosts that credential path has to reach (doctor checks the
    policy for each). ``missing_token_detail`` is doctor's row text and
    ``missing_token_error`` provisioning's failure — kept as literal
    strings per backend rather than templated so the copilot wording never
    drifts.
    """

    name: str
    label: str
    token_env: str
    token_host: str
    token_hosts: tuple[str, ...]
    credential: str
    create_url: str
    missing_token_detail: str
    missing_token_error: str
    models_source: str

    @property
    def is_default(self) -> bool:
        return self.name == "copilot"

    @property
    def secret(self) -> tuple[str, str]:
        """The ``(env, host)`` custom-secret registration this backend owns."""
        return (self.token_env, self.token_host)

    @property
    def doctor_check_name(self) -> str:
        """The credential row's name: bare for the default backend, tagged
        with the backend otherwise so a reader sees *why* it is the row."""
        if self.is_default:
            return self.token_env
        return f"{self.token_env} (agent backend: {self.name})"

    def has_token(self, env: dict[str, str]) -> bool:
        return bool(env.get(self.token_env))


COPILOT = AgentBackend(
    name="copilot",
    label="copilot",
    token_env=COPILOT_TOKEN_ENV,
    token_host=COPILOT_TOKEN_HOST,
    token_hosts=("api.githubcopilot.com", "api.github.com"),
    credential='a fine-grained PAT with the "Copilot Requests" permission',
    create_url="https://github.com/settings/personal-access-tokens",
    missing_token_detail=(
        'not set — create a fine-grained PAT with the "Copilot Requests" '
        f"permission and export {COPILOT_TOKEN_ENV}"
    ),
    missing_token_error=(
        f"{COPILOT_TOKEN_ENV} is not set on the host. Create a fine-grained PAT "
        'with the "Copilot Requests" permission and export it.'
    ),
    models_source="the SDK",
)

CLAUDE = AgentBackend(
    name="claude",
    label="claude",
    token_env=ANTHROPIC_TOKEN_ENV,
    token_host=ANTHROPIC_TOKEN_HOST,
    token_hosts=(ANTHROPIC_TOKEN_HOST,),
    credential="an Anthropic API key",
    create_url="https://console.anthropic.com/settings/keys",
    missing_token_detail=(
        "not set — create an Anthropic API key and export "
        f'{ANTHROPIC_TOKEN_ENV}, or switch [agent] backend back to "copilot"'
    ),
    missing_token_error=(
        f'{ANTHROPIC_TOKEN_ENV} is not set on the host but [agent] backend = "claude". '
        "Create an Anthropic API key and export it, or switch back to "
        'backend = "copilot".'
    ),
    models_source="the Anthropic Models API",
)

#: Every backend ``[agent] backend`` accepts, default first. The config
#: Literal and ``daemon.discord_format.KNOWN_BACKENDS`` name the same set.
BACKENDS: tuple[AgentBackend, ...] = (COPILOT, CLAUDE)

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
