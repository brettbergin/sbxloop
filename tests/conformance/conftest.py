from __future__ import annotations

from dataclasses import dataclass

import pytest

from sbxloop.vcs.protocol import VcsOps
from tests.conformance.backends import BACKENDS, Backend, Seeds, registered
from tests.conformance.gate import check_needs


@dataclass
class Subject:
    """What a scenario is handed: the backend object, where its repository
    is, and the seeds to arrange state in it."""

    backend: Backend
    ops: VcsOps
    seeds: Seeds

    @property
    def kind(self) -> str:
        return self.backend.kind

    @property
    def repo(self) -> str:
        return self.backend.repo

    @property
    def base(self) -> str:
        return self.backend.base


@pytest.fixture(params=registered())
def subject(request: pytest.FixtureRequest) -> Subject:
    backend = BACKENDS[request.param]
    reason = backend.unavailable()
    if reason is not None:
        pytest.skip(reason)
    ops = backend.make()
    needs: set[str] = set()
    for marker in request.node.iter_markers("needs"):
        needs.update(str(name) for name in marker.args)
    check_needs(backend.kind, ops.capabilities(), needs)
    return Subject(backend=backend, ops=ops, seeds=backend.seeds(ops))
