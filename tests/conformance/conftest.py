from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from sbxloop.errors import RoleNotImplemented
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


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    """A backend that has not landed a role yet (#1017) raises
    :class:`RoleNotImplemented` at the role's first operation. In the
    suite that is a skip naming the operation, not a failure: the
    scenario was written for a backend that answers every role, and this
    one says which it does not yet — the same way an ``UNSUPPORTED``
    capability skips with its name."""
    outcome = yield
    try:
        outcome.get_result()
    except RoleNotImplemented as exc:
        outcome.force_exception(pytest.skip.Exception(str(exc)))
