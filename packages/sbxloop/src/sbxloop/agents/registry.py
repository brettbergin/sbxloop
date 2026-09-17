"""Where agents are looked up: the built-ins, the operator's ``[[agents]]``,
then the agents people saved.

:class:`AgentRegistry` is the one interface the platform and the engine ask.
:class:`ConfigAgentRegistry` answers it from the shipped catalogue merged
with ``sbxloop.toml``; it is read-only. :class:`DbAgentRegistry` layers a
person's own agents, stored in the ``agents`` table, after those two: a
stored agent never takes a slug or alias a built-in or configured agent
already has, and only stored agents are created, edited or archived.
"""

from __future__ import annotations

import builtins
import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError
from sqlalchemy import insert, select

from sbxloop.agents.builtin import BUILTIN_AGENTS, PRIMARY_BUILTINS
from sbxloop.agents.definition import AgentDefinition, AgentRoleName, AgentSpec
from sbxloop.agents.tools import TOOL_CATALOG
from sbxloop.db.api_models import ApiEventRow
from sbxloop.db.collaboration_models import AgentRow, TeamRow
from sbxloop.errors import SbxloopError
from sbxloop.log import get_logger

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from sbxloop.config import Config
    from sbxloop.daemon.store import DaemonStore

__all__ = [
    "AgentExists",
    "AgentInvalid",
    "AgentNotFound",
    "AgentReadOnly",
    "AgentRegistry",
    "AgentRegistryReadOnly",
    "AgentRevisionConflict",
    "AgentSlugTaken",
    "ConfigAgentRegistry",
    "DbAgentRegistry",
    "addressable",
    "catalog_model_problems",
    "config_agent_problems",
    "default_registry",
]

log = get_logger(__name__)


#: The built-ins an entry may adjust without naming the agent again.
_PRIMARY_SLUGS = frozenset(agent.slug for agent in PRIMARY_BUILTINS)


class AgentRegistryReadOnly(SbxloopError):
    """The registry (or the agent) cannot be changed from here."""


#: The name the shared contract uses for the same refusal.
AgentReadOnly = AgentRegistryReadOnly


class AgentNotFound(SbxloopError):
    """No stored agent has that slug."""


class AgentExists(SbxloopError):
    """A stored agent already has that slug."""


class AgentSlugTaken(SbxloopError):
    """A team already answers to a name the agent would take."""


class AgentArchived(SbxloopError):
    """The stored agent was archived and is no longer edited."""


class AgentRevisionConflict(SbxloopError):
    """The agent changed since the revision the caller acted on."""

    def __init__(self, slug: str, expected: int, current: int) -> None:
        super().__init__(
            f"agent {slug!r} is at revision {current}, not {expected}; reload it and retry"
        )
        self.slug = slug
        self.expected = expected
        self.current = current


class AgentInvalid(SbxloopError):
    """The spec cannot be saved; ``problems`` says why."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = builtins.list(problems)
        super().__init__("; ".join(self.problems))


class AgentRegistry(Protocol):
    def get(self, slug_or_alias: str) -> AgentDefinition | None:
        """The agent a slug or alias names, enabled or not; case-insensitive."""

    def list(self, include_disabled: bool = False) -> builtins.list[AgentDefinition]:
        """The agents clients show: enabled and current, built-ins first.
        ``include_disabled`` adds the disabled and the legacy names."""

    def for_role(self, role: AgentRoleName) -> builtins.list[AgentDefinition]:
        """The enabled agents that may take ``role``, built-ins first."""

    def create(self, spec: AgentSpec, by: str) -> AgentDefinition: ...

    def update(
        self, slug: str, patch: Mapping[str, Any], expected_revision: int, by: str
    ) -> AgentDefinition: ...

    def archive(self, slug: str, by: str) -> AgentDefinition: ...

    def validate(self, spec: AgentSpec) -> builtins.list[str]:
        """Everything wrong with ``spec`` beside the agents already known;
        empty when it may be saved."""


def _merge(base: AgentDefinition | None, spec: AgentSpec) -> AgentDefinition:
    """``spec`` over ``base``: a configured entry for a current built-in
    changes only the keys it sets to something other than their default.
    A legacy name is kept only so old saved teams resolve, so an entry with
    that slug replaces it outright and is listed like any added agent."""
    if base is None or base.legacy:
        return AgentDefinition(spec, "config")
    merged = AgentSpec.model_validate(
        {**base.spec.model_dump(), **spec.model_dump(exclude_defaults=True)}
    )
    return AgentDefinition(
        merged,
        "config",
        revision=base.revision,
        category=base.category,
        capabilities=base.capabilities,
    )


def _problems(
    spec: AgentSpec,
    others: Iterable[AgentDefinition],
    *,
    credentials: set[str],
    mcp: set[str],
) -> builtins.list[str]:
    label = f"agent {spec.slug!r}"
    problems: builtins.list[str] = []
    if not spec.name and spec.slug not in _PRIMARY_SLUGS:
        problems.append(f"{label} needs a name")
    unknown_tools = sorted(set(spec.tools or ()) - TOOL_CATALOG - mcp)
    if unknown_tools:
        problems.append(f"{label} names unknown tool(s) {unknown_tools}")
    unknown_credentials = sorted(set(spec.credentials) - credentials)
    if unknown_credentials:
        problems.append(
            f"{label} names credential(s) {unknown_credentials} not declared under [[credentials]]"
        )
    unknown_mcp = sorted(set(spec.mcp or ()) - mcp)
    if unknown_mcp:
        problems.append(f"{label} names mcp server(s) {unknown_mcp} not declared under [[mcp]]")
    if spec.can_start and not spec.roles:
        problems.append(f"{label} sets can_start but takes no roles")
    owners: dict[str, str] = {}
    for other in others:
        if other.slug == spec.slug:
            continue
        owners[other.slug] = other.slug
        for alias in other.spec.aliases:
            owners.setdefault(alias, other.slug)
    if spec.slug in owners:
        problems.append(f"{label}: {spec.slug!r} is already an alias of {owners[spec.slug]!r}")
    for alias in spec.aliases:
        if alias in owners:
            problems.append(f"{label}: alias {alias!r} already names agent {owners[alias]!r}")
    return problems


class ConfigAgentRegistry:
    """The built-in catalogue with ``[[agents]]`` merged in by slug."""

    def __init__(self, config: Config | None = None) -> None:
        self._credentials = {c.name for c in config.credentials} if config else set()
        self._mcp = {m.name for m in config.mcp} if config else set()
        entries: dict[str, AgentDefinition] = {a.slug: a for a in BUILTIN_AGENTS}
        for spec in config.agents if config else ():
            entries[spec.slug] = _merge(entries.get(spec.slug), spec)
        self._entries = entries
        self._aliases: dict[str, str] = {}
        for agent in entries.values():
            for alias in agent.spec.aliases:
                self._aliases.setdefault(alias, agent.slug)

    @property
    def credential_names(self) -> set[str]:
        """The ``[[credentials]]`` an agent may name."""
        return self._credentials

    @property
    def mcp_names(self) -> set[str]:
        """The ``[[mcp]]`` servers an agent may name."""
        return self._mcp

    def get(self, slug_or_alias: str) -> AgentDefinition | None:
        key = slug_or_alias.strip().casefold()
        agent = self._entries.get(key)
        if agent is None and key in self._aliases:
            agent = self._entries[self._aliases[key]]
        return agent

    def list(self, include_disabled: bool = False) -> builtins.list[AgentDefinition]:
        return [
            agent
            for agent in self._entries.values()
            if include_disabled or (agent.spec.enabled and not agent.legacy)
        ]

    def for_role(self, role: AgentRoleName) -> builtins.list[AgentDefinition]:
        return [agent for agent in self.list() if role in agent.spec.roles]

    def create(self, spec: AgentSpec, by: str) -> AgentDefinition:
        raise AgentRegistryReadOnly("agents come from the built-ins and sbxloop.toml here")

    def update(
        self, slug: str, patch: Mapping[str, Any], expected_revision: int, by: str
    ) -> AgentDefinition:
        raise AgentRegistryReadOnly(f"agent {slug!r} is defined by sbxloop or sbxloop.toml")

    def archive(self, slug: str, by: str) -> AgentDefinition:
        raise AgentRegistryReadOnly(f"agent {slug!r} is defined by sbxloop or sbxloop.toml")

    def validate(self, spec: AgentSpec) -> builtins.list[str]:
        return _problems(spec, self._entries.values(), credentials=self._credentials, mcp=self._mcp)


def config_agent_problems(config: Config) -> builtins.list[str]:
    """What is wrong with ``config``'s ``[[agents]]``, each entry judged as
    it resolves (merged over the built-in it adjusts)."""
    registry = ConfigAgentRegistry(config)
    problems: builtins.list[str] = []
    for spec in config.agents:
        resolved = registry.get(spec.slug)
        if resolved is not None:
            problems += registry.validate(resolved.spec)
    return problems


def addressable(agent: AgentDefinition | None, slug: str) -> bool:
    """Whether ``slug`` names an agent a team, a mention or a handoff may
    reach: its own slug (not an alias), enabled, and not archived."""
    return agent is not None and agent.slug == slug and agent.active


def default_registry(config: Config) -> AgentRegistry:
    """The registry a daemon without stored agents uses."""
    return ConfigAgentRegistry(config)


def catalog_model_problems(config: Config, spec: AgentSpec) -> builtins.list[str]:
    """``spec.model`` against the configured provider's discovered models.

    Only a cached catalog can refuse a model: until the backend has listed
    its models (or when it cannot), a named model is taken as given and the
    run that uses it is where a wrong name surfaces.
    """
    if spec.model is None:
        return []
    from sbxloop.backends import backend_for
    from sbxloop.modelcatalog import catalog_endpoint, load_catalog

    backend = backend_for(config)
    catalog = load_catalog(config.paths, backend, endpoint=catalog_endpoint(config))
    if catalog is None or any(model.id == spec.model for model in catalog.models):
        return []
    return [
        f"agent {spec.slug!r}: model {spec.model!r} is not one the {backend.name} provider lists"
    ]


def _validation_messages(exc: ValidationError) -> builtins.list[str]:
    messages = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"])
        messages.append(f"{where}: {error['msg']}" if where else str(error["msg"]))
    return messages


class DbAgentRegistry:
    """The built-ins and ``[[agents]]``, then the agents people saved.

    Stored agents are ``source="user"``: created at revision 1, every edit
    and the archive bump it, and an edit names the revision it was made
    against. Every check against the agents already known runs inside the
    write transaction, so two saves cannot both claim one name.
    """

    def __init__(
        self,
        config: Config,
        dstore: DaemonStore,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._base = ConfigAgentRegistry(config)
        self._dstore = dstore
        self._clock = clock

    # -- reads -------------------------------------------------------------------

    def _definition(self, row: AgentRow) -> AgentDefinition | None:
        try:
            spec = AgentSpec.model_validate_json(row.spec_json)
        except ValidationError:
            log.warning("agents.stored_spec_unreadable", slug=str(row.slug))
            return None
        if spec.slug != row.slug or self._base.get(spec.slug) is not None:
            # A configured agent took the name after this one was saved:
            # the configured one wins, and this one is not served.
            return None
        return AgentDefinition(
            spec, "user", revision=int(row.revision), archived=row.state == "archived"
        )

    def _stored(self, session: Session) -> builtins.list[AgentDefinition]:
        rows = session.scalars(select(AgentRow).order_by(AgentRow.created_at, AgentRow.slug))
        stored = []
        for row in rows:
            agent = self._definition(row)
            if agent is not None:
                stored.append(agent)
        return stored

    def get(self, slug_or_alias: str) -> AgentDefinition | None:
        agent = self._base.get(slug_or_alias)
        if agent is not None:
            return agent
        key = slug_or_alias.strip().casefold()
        with self._dstore.read() as session:
            stored = self._stored(session)
        for candidate in stored:
            if candidate.slug == key:
                return candidate
        return next((c for c in stored if key in c.spec.aliases), None)

    def list(self, include_disabled: bool = False) -> builtins.list[AgentDefinition]:
        with self._dstore.read() as session:
            stored = self._stored(session)
        return [
            *self._base.list(include_disabled),
            *(agent for agent in stored if include_disabled or agent.active),
        ]

    def for_role(self, role: AgentRoleName) -> builtins.list[AgentDefinition]:
        return [agent for agent in self.list() if role in agent.spec.roles]

    # -- validation --------------------------------------------------------------

    def _problems(self, session: Session, spec: AgentSpec) -> builtins.list[str]:
        known = [*self._base.list(include_disabled=True), *self._stored(session)]
        return _problems(
            spec,
            known,
            credentials=self._base.credential_names,
            mcp=self._base.mcp_names,
        )

    def validate(self, spec: AgentSpec) -> builtins.list[str]:
        with self._dstore.read() as session:
            problems = self._problems(session, spec)
        return problems + catalog_model_problems(self._config, spec)

    # -- writes ------------------------------------------------------------------

    def _refuse_configured(self, slug: str) -> None:
        if self._base.get(slug) is not None:
            raise AgentRegistryReadOnly(
                f"agent {slug!r} is defined by sbxloop or sbxloop.toml and cannot be changed here"
            )

    @staticmethod
    def _refuse_team_names(session: Session, spec: AgentSpec) -> None:
        """A mention resolves an agent before a team, so an agent may not
        take a name any team already has."""
        names = {spec.slug, *spec.aliases}
        taken = sorted(
            {str(slug) for slug in session.scalars(select(TeamRow.slug)) if slug in names}
        )
        if taken:
            raise AgentSlugTaken(f"a team is already called {', '.join(taken)}")

    @staticmethod
    def _event(session: Session, type_: str, slug: str, now: float, by: str) -> None:
        session.execute(
            insert(ApiEventRow).values(
                recorded_at=now,
                occurred_at=now,
                type=type_,
                run_id=None,
                item_id=None,
                operation_id=None,
                actor_json=None,
                source_seq=None,
                data_json=json.dumps({"slug": slug, "by": by}),
            )
        )

    def create(self, spec: AgentSpec, by: str) -> AgentDefinition:
        problems: builtins.list[str] = []
        if self._base.get(spec.slug) is not None:
            problems.append(
                f"agent {spec.slug!r}: the name is taken by a built-in or sbxloop.toml agent"
            )
        problems += catalog_model_problems(self._config, spec)
        now = self._clock()
        with self._dstore.transaction() as session:
            if not problems and session.get(AgentRow, spec.slug) is not None:
                raise AgentExists(f"agent {spec.slug!r} already exists")
            self._refuse_team_names(session, spec)
            problems = self._problems(session, spec) + problems
            if problems:
                raise AgentInvalid(problems)
            session.add(
                AgentRow(
                    slug=spec.slug,
                    spec_json=spec.model_dump_json(),
                    state="active",
                    created_by=by or None,
                    created_at=now,
                    updated_at=now,
                    revision=1,
                )
            )
            self._event(session, "agent.created", spec.slug, now, by)
        return AgentDefinition(spec, "user", revision=1)

    def update(
        self, slug: str, patch: Mapping[str, Any], expected_revision: int, by: str
    ) -> AgentDefinition:
        key = slug.strip().casefold()
        self._refuse_configured(key)
        values = dict(patch)
        if values.get("slug", key) != key:
            raise AgentInvalid([f"agent {key!r}: the slug cannot change"])
        now = self._clock()
        with self._dstore.transaction() as session:
            row = session.get(AgentRow, key)
            if row is None:
                raise AgentNotFound(f"agent {key!r} not found")
            if row.state != "active":
                raise AgentArchived(f"agent {key!r} is archived")
            if int(row.revision) != expected_revision:
                raise AgentRevisionConflict(key, expected_revision, int(row.revision))
            current = AgentSpec.model_validate_json(row.spec_json)
            try:
                spec = AgentSpec.model_validate({**current.model_dump(), **values, "slug": key})
            except ValidationError as exc:
                raise AgentInvalid(_validation_messages(exc)) from exc
            if set(spec.aliases) - set(current.aliases):
                self._refuse_team_names(session, spec)
            problems = self._problems(session, spec)
            if spec.model != current.model:
                problems += catalog_model_problems(self._config, spec)
            if problems:
                raise AgentInvalid(problems)
            row.spec_json = spec.model_dump_json()
            row.updated_at = now
            row.revision = int(row.revision) + 1
            revision = int(row.revision)
            self._event(session, "agent.updated", key, now, by)
        return AgentDefinition(spec, "user", revision=revision)

    def archive(self, slug: str, by: str) -> AgentDefinition:
        key = slug.strip().casefold()
        self._refuse_configured(key)
        now = self._clock()
        with self._dstore.transaction() as session:
            row = session.get(AgentRow, key)
            if row is None:
                raise AgentNotFound(f"agent {key!r} not found")
            spec = AgentSpec.model_validate_json(row.spec_json)
            if row.state != "archived":
                row.state = "archived"
                row.updated_at = now
                row.revision = int(row.revision) + 1
                self._event(session, "agent.archived", key, now, by)
            revision = int(row.revision)
        return AgentDefinition(spec, "user", revision=revision, archived=True)
