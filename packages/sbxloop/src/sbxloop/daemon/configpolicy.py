"""Which configuration keys chat may never change (#970, #971).

The concierge's config tools run in the daemon process against the
operator's own file, with the same authority as ``!sbx``. Two things bound
that. A key that could sever the channel the outcome is reported on — the
chat sections, the concierge's own switch — is refused from chat whatever
the operator's lock list says, because a wrong value there leaves nobody
able to see the report or ask for the revert. And the keys the loader
takes from the environment alone are refused because a file line would do
nothing. Everything else is the operator's to lock or unlock (#971).
"""

from __future__ import annotations

import re

from sbxloop.configedit import keys as configkeys

_INDEX = re.compile(r"\[\d+\]")

#: Dotted prefixes chat may read but never write, with the reason the
#: refusal quotes. A prefix matches the key itself and everything under it.
NEVER_FROM_CHAT: dict[str, str] = {
    "chat": "it selects the chat backend the outcome is reported on",
    "discord": "it configures the chat channel the outcome is reported on",
    "slack": "it configures the chat channel the outcome is reported on",
    "mattermost": "it configures the chat channel the outcome is reported on",
    "tui": "it configures the operator console's own bridge",
    "concierge.enabled": "it is the concierge's own switch",
    "concierge.edit_config": "it is the gate on these tools — no self-widening",
    "concierge.config_locked": "it is the lock list these tools honour — no self-widening",
}


def _bare(dotted: str) -> str:
    return _INDEX.sub("", dotted)


def _under(key: str, prefix: str) -> bool:
    return key == prefix or key.startswith(prefix + ".")


def never_from_chat(dotted: str) -> str | None:
    """The reason ``dotted`` is never changed from chat, or ``None``."""
    key = _bare(dotted)
    why = configkeys.ENV_ONLY_KEYS.get(key)
    if why is not None:
        return f"not a file setting: {why}"
    for prefix, reason in NEVER_FROM_CHAT.items():
        if _under(key, prefix):
            return reason
    return None


__all__ = ["NEVER_FROM_CHAT", "never_from_chat"]
