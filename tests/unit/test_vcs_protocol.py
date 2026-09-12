"""The role protocols are the whole of a backend, and the GitHub backend
answers every one of them (#1011).

Three things hold the seam: every public operation on the GitHub backend
belongs to exactly one role (a method that belongs to none is a path a
second backend would not know to implement; one that belongs to two is a
role boundary drawn wrong); the backend satisfies each role structurally,
at runtime as ``mypy`` proves it statically; and the capability report
names every capability with one of the three states, never a boolean."""

from __future__ import annotations

import inspect

from sbxloop.vcs.github.ops import GithubOps
from sbxloop.vcs.protocol import (
    CAPABILITIES,
    ROLES,
    Capability,
    VcsOps,
)
from tests.fakes.fake_github import FakeGithub
from tests.fakes.ops_stub import OpsStub

# The transport, private to the backend package (``test_vcs_raw_is_private``)
# and so no role's business.
TRANSPORT = {"raw", "raw_lookup"}


def _public_methods(cls: type) -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def _role_methods(role: type) -> set[str]:
    return {name for name in role.__protocol_attrs__ if not name.startswith("_")}  # type: ignore[attr-defined]


class TestRolesPartitionTheBackend:
    def test_every_operation_belongs_to_exactly_one_role(self) -> None:
        seen: dict[str, str] = {}
        for role in ROLES:
            for name in _role_methods(role):
                assert name not in seen, f"{name} is on both {seen[name]} and {role.__name__}"
                seen[name] = role.__name__
        operations = _public_methods(GithubOps) - TRANSPORT
        unplaced = sorted(operations - set(seen))
        assert unplaced == [], f"operations on no role: {unplaced}"
        phantom = sorted(set(seen) - operations)
        assert phantom == [], f"role methods the backend lacks: {phantom}"

    def test_the_composite_is_the_union_of_the_roles(self) -> None:
        union = set().union(*(_role_methods(role) for role in ROLES))
        assert _role_methods(VcsOps) == union


class TestTheGithubBackendAnswersEveryRole:
    def test_structurally_at_runtime(self) -> None:
        fake = FakeGithub()
        for role in ROLES:
            assert isinstance(fake, role), role.__name__
        assert isinstance(fake, VcsOps)

    def test_a_stand_in_inherits_the_roles(self) -> None:
        assert isinstance(OpsStub(), VcsOps)


class TestCapabilities:
    def test_github_reports_every_capability_in_one_of_three_states(self) -> None:
        report = FakeGithub().capabilities()
        assert set(report) == set(CAPABILITIES)
        assert all(isinstance(state, Capability) for state in report.values())
        assert all(not isinstance(state, bool) for state in report.values())

    def test_github_does_what_the_roles_rely_on(self) -> None:
        report = GithubOps.CAPABILITIES
        supported = {name for name, state in report.items() if state is Capability.SUPPORTED}
        assert supported == set(CAPABILITIES) - {"signed_api_commits"}

    def test_signed_commits_are_the_credentials_business_not_the_transports(self) -> None:
        # A GitHub App's API commits arrive signed; a PAT's do not. The ops
        # object does not know which it holds, so it cannot answer — and an
        # answer it cannot give is UNKNOWN, never a guess either way.
        assert GithubOps.CAPABILITIES["signed_api_commits"] is Capability.UNKNOWN

    def test_the_report_is_a_copy(self) -> None:
        fake = FakeGithub()
        fake.capabilities()["merge_queue"] = Capability.UNSUPPORTED
        assert fake.capabilities()["merge_queue"] is Capability.SUPPORTED
