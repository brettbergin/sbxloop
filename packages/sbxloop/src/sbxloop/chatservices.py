"""The chat service descriptor (#930).

``[chat] backend`` picks which service carries the daemon's human channel,
and everything on the host that is *about* that choice reads it from here:
which config section holds its settings, which env vars carry its
credentials, which optional extra installs its SDK, and which module holds
its :class:`~sbxloop.daemon.chat.ChatBridge`. ``build_bridge``, ``doctor``
and ``Config.chat_section`` consult :func:`service_named` instead of
branching on "discord, else slack", so a third service is a row here rather
than an ``if`` in four modules.

This is the chat twin of :mod:`sbxloop.backends`, which does the same job
for agent backends, and it follows the same rule: nothing from the config
package is imported at runtime (only the type), so the low-level modules
that need the credential constants never form an import cycle.

The Discord and Slack descriptors carry the exact strings those commands
printed before the descriptor existed — an existing deployment reads
byte-identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sbxloop.daemon.chat import ChatBridge

DISCORD_TOKEN_ENV = "DISCORD_BOT_TOKEN"  # nosec B105 - env var name, not a secret
SLACK_BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"  # nosec B105 - env var name, not a secret
SLACK_APP_TOKEN_ENV = "SLACK_APP_TOKEN"  # nosec B105 - env var name, not a secret
MATTERMOST_TOKEN_ENV = "MATTERMOST_BOT_TOKEN"  # nosec B105 - env var name, not a secret


@dataclass(frozen=True)
class ChatService:
    """One external chat service as the host sees it.

    ``section`` is the ``Config`` attribute holding its settings, and also
    the ``[section]`` name an operator writes in sbxloop.toml — the two are
    the same word on every service, which is what lets ``chat_section``
    resolve by name. ``token_envs`` are the credentials the bridge needs in
    the environment (doctor reports each missing one); ``import_name`` is
    the module doctor imports to prove the extra is installed and
    ``sdk_label`` what it calls that SDK in the row — the two differ
    wherever a distribution and its module are spelled differently — and
    ``extra`` names that extra in the hint it prints.
    """

    name: str
    label: str
    section: str
    token_envs: tuple[str, ...]
    extra: str
    import_name: str
    sdk_label: str
    module: str
    bridge_class: str

    @property
    def missing_extra_detail(self) -> str:
        """Doctor's wording when the SDK is not importable."""
        return f"{self.sdk_label} missing (pip install 'sbxloop[{self.extra}]')"

    def bridge_type(self) -> type[ChatBridge]:
        """The bridge class, imported now. The backend module — and its
        optional extra — is imported only when that backend is chosen, so a
        Discord deployment never loads another service's SDK."""
        import importlib

        module = importlib.import_module(self.module)
        bridge: type[ChatBridge] = getattr(module, self.bridge_class)
        return bridge


DISCORD = ChatService(
    name="discord",
    label="Discord",
    section="discord",
    token_envs=(DISCORD_TOKEN_ENV,),
    extra="discord",
    import_name="discord",
    sdk_label="discord.py",
    module="sbxloop.daemon.discord",
    bridge_class="DiscordBridge",
)

SLACK = ChatService(
    name="slack",
    label="Slack",
    section="slack",
    token_envs=(SLACK_BOT_TOKEN_ENV, SLACK_APP_TOKEN_ENV),
    extra="slack",
    import_name="slack_sdk",
    sdk_label="slack_sdk",
    module="sbxloop.daemon.slack",
    bridge_class="SlackBridge",
)

MATTERMOST = ChatService(
    name="mattermost",
    label="Mattermost",
    section="mattermost",
    token_envs=(MATTERMOST_TOKEN_ENV,),
    extra="mattermost",
    import_name="aiohttp",
    sdk_label="aiohttp",
    module="sbxloop.daemon.mattermost",
    bridge_class="MattermostBridge",
)

#: Every service ``[chat] backend`` accepts. The ``ChatBackend`` Literal in
#: the config package names the same set; ``test_config_chat`` pins the two
#: together.
CHAT_SERVICES: tuple[ChatService, ...] = (DISCORD, SLACK, MATTERMOST)

#: Every bridge the daemon can run, the always-on local one included. The
#: ``BridgeBackend`` Literal names the same set.
LOCAL_BACKEND = "local"

_BY_NAME = {service.name: service for service in CHAT_SERVICES}


def service_named(name: str) -> ChatService:
    """The descriptor for ``name``; an unknown name is a programming error
    (config validation already limits the Literal), reported as such."""
    try:
        return _BY_NAME[name]
    except KeyError:
        known = ", ".join(service.name for service in CHAT_SERVICES)
        raise ValueError(f"unknown chat backend {name!r} (known: {known})") from None


def service_for(config: Any) -> ChatService | None:
    """The descriptor ``[chat] backend`` selects, or None when the daemon
    runs headless."""
    backend = config.chat_backend
    return None if backend is None else service_named(backend)


__all__ = [
    "CHAT_SERVICES",
    "DISCORD",
    "DISCORD_TOKEN_ENV",
    "LOCAL_BACKEND",
    "MATTERMOST",
    "MATTERMOST_TOKEN_ENV",
    "SLACK",
    "SLACK_APP_TOKEN_ENV",
    "SLACK_BOT_TOKEN_ENV",
    "ChatService",
    "service_for",
    "service_named",
]
