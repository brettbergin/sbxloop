"""What one agent is: its spec (identity, persona, narrowing) and where it came from.

An :class:`AgentSpec` is what an operator writes under ``[[agents]]`` (and,
later, what a person saves from the agent editor). It carries identity,
persona and narrowing only: no egress, no hosts, no allow patterns. Egress
stays the operator's ``[policy]`` and ``[[workloads]]``; an agent names
``[[credentials]]`` and ``[[mcp]]`` entries, never hosts.

An :class:`AgentDefinition` is a resolved spec plus its source and revision,
and the two persona blocks the chat session and a run are given.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "AGENT_SLUG_PATTERN",
    "AgentDefinition",
    "AgentRoleName",
    "AgentSource",
    "AgentSpec",
    "AgentStartKind",
]

#: The roles an agent may take in a run. ``lead`` is the product persona that
#: answers the channel and coordinates; the rest are the run phases' roles.
AgentRoleName = Literal["lead", "planner", "builder", "critic", "operator"]
#: The run kinds an agent may start by itself.
AgentStartKind = Literal["code", "workload"]
#: Where a definition came from: shipped, the operator's toml, or a person.
AgentSource = Literal["builtin", "config", "user"]

AGENT_SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
_SLUG_RE = re.compile(AGENT_SLUG_PATTERN)
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
#: A multi-character avatar must be one emoji: pictographs plus the joiners,
#: variation selectors and modifiers that compose them. The longest real
#: sequences stay well under this.
_MAX_EMOJI_CODEPOINTS = 16
_EMOJI_CATEGORIES = {"So", "Sk", "Mn", "Me"}
_ZWJ = "\u200d"


def _is_emoji(value: str) -> bool:
    if len(value) > _MAX_EMOJI_CODEPOINTS:
        return False
    pictographs = 0
    for char in value:
        if char == _ZWJ:
            continue
        category = unicodedata.category(char)
        if category not in _EMOJI_CATEGORIES:
            return False
        pictographs += category == "So"
    return pictographs > 0


def _printable(value: str) -> bool:
    return all(
        char == _ZWJ or not unicodedata.category(char).startswith(("C", "Z")) for char in value
    )


class AgentSpec(BaseModel):
    """One agent as configured. ``extra="forbid"``: an egress key is refused.

    Every field but ``slug`` has a default so a ``[[agents]]`` entry can
    adjust a built-in by naming only what changes; a new agent must also
    have a ``name`` (the registry checks that).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str
    name: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=500)
    # An emoji or at most two characters, never a URL: pages load no
    # third-party images, and a person's initials stand in for a picture.
    avatar: str = ""
    # `#rrggbb`; clients render it through their own styling.
    color: str = "#10b981"
    # The persona: what the agent is told in chat and, for a custom agent,
    # at the top of each run session it takes.
    instructions: str = Field(default="", max_length=8000)
    # None: the model the role gets today.
    model: str | None = None
    roles: list[AgentRoleName] = Field(default_factory=list)
    # None: what the role gets today. A list only narrows it.
    tools: list[str] | None = None
    skills: list[str] | None = None
    # `[[mcp]]` names, never hosts.
    mcp: list[str] | None = None
    # `[[credentials]]` names.
    credentials: list[str] = Field(default_factory=list)
    # Topics the agent may speak up about unprompted.
    interests: list[str] = Field(default_factory=list)
    can_start: list[AgentStartKind] = Field(default_factory=list)
    max_runs_per_day: int | None = Field(default=None, ge=0)
    aliases: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        if not _SLUG_RE.match(value):
            raise ValueError(
                "agent slug must be 1-64 lowercase letters, digits, '-' or '_', "
                f"starting with a letter or digit, got {value!r}"
            )
        return value

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _printable(value.replace(" ", "")):
            raise ValueError(f"agent name must be printable text, got {value!r}")
        return value.strip()

    @field_validator("avatar")
    @classmethod
    def _check_avatar(cls, value: str) -> str:
        if not value:
            return value
        url_free = "/" not in value and ":" not in value
        if url_free and _printable(value) and (len(value) <= 2 or _is_emoji(value)):
            return value
        raise ValueError(
            f"agent avatar must be an emoji or 1-2 characters (never a URL), got {value!r}"
        )

    @field_validator("color")
    @classmethod
    def _check_color(cls, value: str) -> str:
        if not _COLOR_RE.match(value):
            raise ValueError(f"agent color must be a '#rrggbb' hex colour, got {value!r}")
        return value.lower()

    @field_validator("model")
    @classmethod
    def _check_model(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("agent model must name a model, or be left out")
        return value

    @field_validator("aliases")
    @classmethod
    def _check_aliases(cls, value: list[str]) -> list[str]:
        bad = [alias for alias in value if not _SLUG_RE.match(alias)]
        if bad:
            raise ValueError(f"agent aliases must be slugs, got {bad!r}")
        return list(dict.fromkeys(value))

    @field_validator("roles", "can_start", "credentials", "interests")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @field_validator("tools", "skills", "mcp")
    @classmethod
    def _dedupe_optional(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else list(dict.fromkeys(value))


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """A resolved agent.

    ``legacy``, ``category`` and ``capabilities`` carry the pre-registry
    catalogue's presentation: a legacy agent still resolves by slug but is
    not listed.
    """

    spec: AgentSpec
    source: AgentSource
    revision: int = 0
    legacy: bool = False
    category: str = ""
    capabilities: tuple[str, ...] = ()
    #: A person's agent they archived: still resolves by slug so saved
    #: references can say what it was, but is neither listed nor addressable.
    archived: bool = False

    @property
    def slug(self) -> str:
        return self.spec.slug

    @property
    def active(self) -> bool:
        """Enabled and not archived: what a team or a mention may name."""
        return self.spec.enabled and not self.archived

    @property
    def read_only(self) -> bool:
        """Only a person's own agents are edited through the registry."""
        return self.source != "user"

    def chat_persona(self, product: str | None = None) -> str:
        """The block appended to the chat session's system message;
        ``product`` is the name the product agent answers to (the shipped
        one when not given)."""
        from sbxloop.agents.builtin import CONCIERGE_NAME, chat_persona

        return chat_persona(self, product or CONCIERGE_NAME)

    def run_persona(self) -> str:
        """The block a run session taken by this agent opens with: empty
        when the instructions are a built-in's own, so those runs stay as
        they were."""
        from sbxloop.agents.builtin import run_persona

        return run_persona(self)
