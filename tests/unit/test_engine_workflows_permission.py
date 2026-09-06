"""A delivery the credential may not make ends the run ``blocked`` (#752).

Field, 2026-09-04: a ``.github/workflows`` change under a GitHub App token
without ``workflows: write`` died on an opaque 403 at the tree POST, was
retried after the backoff, and died the same way — 20 minutes and ~80k
tokens for a fact doctor had already printed. The engine now hands such a
run over ``blocked`` with the remedy named, the way a base rule it cannot
satisfy is handed over: no failed attempt, no retry, no breaker count.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sbxloop.engine.engine import LoopEngine
from sbxloop.errors import DeliveryPermissionError, GithubOpsError
from sbxloop.events import HostEventTypes
from tests.conftest import FakeSbx
from tests.fakes.fake_github import FakeGithub
from tests.unit.test_engine import FILES_BUILD, REVIEW_OK, Harness, task, taskgraph


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


REFUSAL = DeliveryPermissionError(
    "delivery touches `.github/workflows/ci.yml` but the credential lacks `workflows: write` "
    "— grant it, then re-queue the item. Nothing was delivered.",
    paths=(".github/workflows/ci.yml",),
    permission="workflows:write",
)


class TestRefusedDeliveryBlocksTheRun:
    def test_the_run_ends_blocked_with_the_remedy_as_its_reason(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeGithub()
        seen: dict[str, Any] = {}

        def refuse(ops: Any, repo: str, **kwargs: Any) -> Any:
            seen["kwargs"] = kwargs
            raise REFUSAL

        monkeypatch.setattr("sbxloop.engine.engine.deliver_workspace", refuse)
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        result = harness.pipeline(fake).start("ship a workflow")

        assert result.state == "blocked"
        assert result.reason is not None and "workflows: write" in result.reason
        assert result.pr_number is None
        # The delivery was handed the grant check to ask lazily.
        assert callable(seen["kwargs"]["workflows_write_granted"])
        # Nothing reached the git data API and the run never went further.
        assert not [p for _, p, _ in fake.raw_calls if "/git/trees" in p or "/git/blobs" in p]
        assert harness.run_states()[-1] == "blocked"
        assert HostEventTypes.RUN_END in harness.event_types()
        assert harness.sandboxes_left() == []


class TestGrantCheck:
    """``_workflows_write_granted`` reads whichever source describes the
    credential, and only when asked."""

    @staticmethod
    def _pipeline(permissions: dict[str, str] | None, scopes: tuple[str, ...] | None) -> Any:
        calls: list[str] = []

        class Provisioner:
            def gh_app_permissions(self, repo: str | None = None) -> dict[str, str] | None:
                calls.append("app")
                return permissions

        class Ops:
            def token_scopes(self) -> tuple[str, ...] | None:
                calls.append("scopes")
                if scopes == ("raise",):
                    raise GithubOpsError("no")
                return scopes

        return SimpleNamespace(provisioner=Provisioner(), ops=Ops(), repo="o/r"), calls

    def _check(self, permissions: Any, scopes: Any) -> tuple[bool | None, list[str]]:
        p, calls = self._pipeline(permissions, scopes)
        check = LoopEngine._workflows_write_granted(object(), p)  # type: ignore[arg-type]
        assert calls == [], "the check is lazy: nothing is asked until the delivery needs it"
        return check(), calls

    def test_an_app_grant_answers_without_asking_for_scopes(self) -> None:
        assert self._check({"workflows": "write"}, ("repo",)) == (True, ["app"])
        assert self._check({"contents": "write"}, ("repo", "workflow")) == (False, ["app"])

    def test_a_pat_answers_from_its_scopes(self) -> None:
        assert self._check(None, ("repo", "workflow")) == (True, ["app", "scopes"])
        assert self._check(None, ("repo",)) == (False, ["app", "scopes"])

    def test_nothing_reported_is_unknown(self) -> None:
        assert self._check(None, None) == (None, ["app", "scopes"])
        # A scope lookup that fails is not a verdict either.
        assert self._check(None, ("raise",)) == (None, ["app", "scopes"])
