"""Where a repository is registered: the daemon's database.

``[[vcs.repos]]`` in sbxloop.toml declared every repository the daemon
worked on, and registering one meant editing the file on the host and
restarting. The registration — which repositories, whether each is
enabled, the branch it delivers to and the forge it lives on — now lives
in the daemon's own database, the way schedules do (#818): the file's
entries are imported at first sight, and from then on the API adds,
changes and removes registrations live, each one a recorded operation.

The file keeps a role. An entry there still carries everything else a
repository can set — labels, templates, a workspace, sandbox packages,
model overrides — and :func:`merge` folds a registration over the entry
of the same name, so an imported repository keeps its settings and a
repository registered from the API gets the defaults. A *new* entry in
the file is registered at the next start; the values of ``enabled`` and
``deliver_base`` in the file are the initial ones, and the database's win
after the import. A removed registration keeps a row, so the file's copy
is not imported again.

What is applied here is the configuration's declared list
(:meth:`Config.replace_repos`): everything that asks the configuration
which repositories exist — intake, the engine's narrowing, the concierge,
the API's catalog — answers for the registry from the moment it changes.
Polling is built once at start from that list, so a registration that
changes what is polled says a restart is needed.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from sbxloop.config import Config, RepoConfig, VcsKind, check_repo_name
from sbxloop.daemon.store import DaemonStore, StoredRepository
from sbxloop.log import get_logger

log = get_logger(__name__)

#: What a registration imported from sbxloop.toml records as its provenance.
CONFIG_SOURCE = "config"

#: The registration fields a change may name.
CHANGEABLE = frozenset({"enabled", "deliver_base"})


def merge(declared: Sequence[RepoConfig], rows: Sequence[StoredRepository]) -> list[RepoConfig]:
    """The effective repository list: one entry per registration, in
    registration order, each the file's entry of that name (its settings
    kept) with the registration's ``kind``, ``enabled`` and
    ``deliver_base`` over it — or a bare entry when the file has none."""
    by_name = {entry.repo.casefold(): entry for entry in declared}
    merged: list[RepoConfig] = []
    for row in rows:
        # A stored kind was validated when it was registered.
        kind = cast(VcsKind | None, row.kind)
        base = by_name.get(row.repo.casefold())
        if base is None:
            merged.append(
                RepoConfig(
                    repo=row.repo, kind=kind, enabled=row.enabled, deliver_base=row.deliver_base
                )
            )
        else:
            merged.append(
                base.model_copy(
                    update={"kind": kind, "enabled": row.enabled, "deliver_base": row.deliver_base}
                )
            )
    return merged


class RepositoryRegistry:
    """The registrations, applied to one configuration.

    One per daemon process, shared by the start-up that builds the polling
    sources and the loop that serves commands: :meth:`activate` imports
    the file's entries once and applies the registry; the mutations apply
    it again. ``ValueError`` from a mutation says why it was refused, in a
    sentence for the person who asked."""

    def __init__(
        self, config: Config, dstore: DaemonStore, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.config = config
        self.dstore = dstore
        self.clock = clock
        self._imported: list[str] | None = None

    def activate(self) -> list[str]:
        """Import every file entry the registry lacks (once per process;
        a removed registration blocks its name), then apply the registry.
        Returns the names imported by this process."""
        if self._imported is None:
            now = self.clock()
            imported: list[str] = []
            for entry in self.config.declared_repos():
                added = self.dstore.add_repository(
                    entry.repo,
                    kind=entry.kind,
                    enabled=entry.enabled,
                    deliver_base=entry.deliver_base,
                    source=CONFIG_SOURCE,
                    by=None,
                    now=now,
                    revive=False,
                )
                if added:
                    imported.append(entry.repo)
                    log.info("repository.imported", repo=entry.repo, enabled=entry.enabled)
                else:
                    log.debug("repository.import_skipped", repo=entry.repo, reason="registered")
            self._imported = imported
        self.apply()
        return list(self._imported)

    def apply(self) -> list[RepoConfig]:
        """The registry over the file's entries, as the configuration's
        declared list from now on."""
        merged = merge(self.config.declared_repos(), self.dstore.repositories())
        self.config.replace_repos(merged)
        return merged

    def add(
        self,
        repo: str,
        *,
        kind: VcsKind | None,
        enabled: bool,
        deliver_base: str | None,
        by: str | None,
        source: str,
    ) -> StoredRepository:
        """Register ``repo``. Refused when the name is not a repository on
        its forge, or is registered already (case-insensitively)."""
        name = repo.strip()
        check_repo_name(name, kind or self.config.vcs.kind)
        existing = self.dstore.repository(name)
        if existing is not None:
            raise ValueError(f"{existing.repo} is already registered")
        added = self.dstore.add_repository(
            name,
            kind=kind,
            enabled=enabled,
            deliver_base=(deliver_base or "").strip() or None,
            source=source,
            by=by,
            now=self.clock(),
            revive=True,
        )
        if not added:  # a race with another registration of the same name
            raise ValueError(f"{name} is already registered")
        self.apply()
        row = self.dstore.repository(name)
        assert row is not None  # just written
        return row

    def update(self, repo: str, changes: Mapping[str, Any], *, by: str | None) -> StoredRepository:
        """Change a registration's ``enabled`` and/or ``deliver_base``;
        anything else named is refused. Unknown repository: ``KeyError``."""
        unknown = sorted(set(changes) - CHANGEABLE)
        if unknown:
            raise ValueError(
                f"a registration's {', '.join(sorted(CHANGEABLE))} can change; "
                f"not {', '.join(unknown)}"
            )
        row = self.dstore.repository(repo)
        if row is None:
            raise KeyError(repo)
        values: dict[str, Any] = {}
        if "enabled" in changes:
            values["enabled"] = bool(changes["enabled"])
        if "deliver_base" in changes:
            base = changes["deliver_base"]
            values["deliver_base"] = str(base).strip() or None if base is not None else None
        if values:
            self.dstore.update_repository(row.repo, now=self.clock(), **values)
            self.apply()
        updated = self.dstore.repository(row.repo)
        assert updated is not None  # updated in place, never removed here
        return updated

    def remove(self, repo: str, *, by: str | None) -> StoredRepository:
        """Forget a registration: the row stays, marked removed, so the
        file's copy is not imported again. Unknown repository: ``KeyError``."""
        row = self.dstore.repository(repo)
        if row is None:
            raise KeyError(repo)
        self.dstore.remove_repository(row.repo, now=self.clock())
        self.apply()
        return row
