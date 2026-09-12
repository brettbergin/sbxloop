"""Opaque public identifiers for the resources a client reads (#1036).

A remote client never sees an issue number, an item id or an ``owner/name``
as an identifier: it sees ``itm_…`` and ``repo_…``, minted here the first
time a resource is read through the API and stable from then on
(``api_public_ids``). A run's public id is ``run_<run id>`` — the run id is
already random and unique, so no row is needed. The internal key of a work
item is the repository *and* the item id together, so two repositories'
issue numbers never alias, and an unknown id resolves to nothing without
saying which kind it was not.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, NamedTuple

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from sbxloop.daemon.controls.principal import WORKSPACE_ID
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.db.api_models import PublicIdRow
from sbxloop.ghids import normalize_item_id
from sbxloop.ids import _token

Kind = Literal["item", "repository", "gate"]

PREFIXES: dict[Kind, str] = {"item": "itm_", "repository": "repo_", "gate": "gate_"}
RUN_PREFIX = "run_"


class Resolved(NamedTuple):
    kind: Kind
    key: str


def item_key(item: WorkItem) -> str:
    """The internal key of a work item: its repository and its canonical
    id, so ``o/one`` issue 7 and ``o/two`` issue 7 are two resources."""
    return f"{item.repo or ''}|{normalize_item_id(item.item_id)}"


def split_item_key(key: str) -> tuple[str | None, str]:
    repo, _, item_id = key.partition("|")
    return (repo or None), item_id


def run_public_id(run_id: str) -> str:
    return RUN_PREFIX + run_id


def parse_run_id(public_id: str) -> str | None:
    if not public_id.startswith(RUN_PREFIX) or len(public_id) <= len(RUN_PREFIX):
        return None
    return public_id[len(RUN_PREFIX) :]


class PublicIds:
    """The mapping, over the daemon's store."""

    def __init__(self, dstore: DaemonStore, *, workspace_id: str = WORKSPACE_ID) -> None:
        self.dstore = dstore
        self.workspace_id = workspace_id

    def assign(self, kind: Kind, keys: Iterable[str], now: float) -> dict[str, str]:
        """``key -> public id`` for every key, minting the ones without
        one in a single write. Insert-or-ignore under the store lock, so
        two readers racing on one resource still agree on its id."""
        wanted = list(dict.fromkeys(keys))
        if not wanted:
            return {}
        with self.dstore.transaction() as session:
            found = self._lookup(session, kind, wanted)
            missing = [key for key in wanted if key not in found]
            if missing:
                session.execute(
                    insert(PublicIdRow)
                    .values(
                        [
                            {
                                "public_id": PREFIXES[kind] + _token(12),
                                "kind": kind,
                                "workspace_id": self.workspace_id,
                                "internal_key": key,
                                "created_at": now,
                            }
                            for key in missing
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=["kind", "workspace_id", "internal_key"])
                )
                found = self._lookup(session, kind, wanted)
        return found

    def _lookup(self, session: object, kind: Kind, keys: list[str]) -> dict[str, str]:
        from sqlalchemy.orm import Session

        assert isinstance(session, Session)  # nosec B101 - the store's session
        rows = session.execute(
            select(PublicIdRow.internal_key, PublicIdRow.public_id).where(
                PublicIdRow.kind == kind,
                PublicIdRow.workspace_id == self.workspace_id,
                PublicIdRow.internal_key.in_(keys),
            )
        )
        return {str(key): str(public) for key, public in rows}

    def item_id(self, item: WorkItem, now: float) -> str:
        return self.assign("item", [item_key(item)], now)[item_key(item)]

    def item_ids(self, items: Iterable[WorkItem], now: float) -> dict[str, str]:
        """``item key -> public id`` for a page of items, one write."""
        return self.assign("item", [item_key(item) for item in items], now)

    def gate_id(self, run_id: str, now: float) -> str:
        """A gate is keyed by the run it parks: one per run."""
        return self.assign("gate", [run_id], now)[run_id]

    def gate_ids(self, run_ids: Iterable[str], now: float) -> dict[str, str]:
        return self.assign("gate", list(run_ids), now)

    def repository_id(self, repo: str, now: float) -> str:
        return self.assign("repository", [repo], now)[repo]

    def repository_ids(self, repos: Iterable[str], now: float) -> dict[str, str]:
        return self.assign("repository", list(repos), now)

    def resolve(self, public_id: str) -> Resolved | None:
        """What a public id names, or ``None`` — for an id of any kind,
        malformed or never issued, alike."""
        with self.dstore.read() as session:
            row = session.get(PublicIdRow, public_id)
        if row is None or row.workspace_id != self.workspace_id:
            return None
        if row.kind not in PREFIXES:
            return None
        kind: Kind = (
            "item" if row.kind == "item" else "gate" if row.kind == "gate" else "repository"
        )
        return Resolved(kind, str(row.internal_key))
