"""The base for a test's own stand-in for :class:`sbxloop.gh.ops.GithubOps`.

A stand-in scripts the generic transport — its ``raw`` answers the paths
it expects and records what it was asked — and inherits every named
operation from the real class, so the code under test calls
``ops.issue_get(...)`` and the stand-in still sees ``GET /repos/o/r/issues/4``
land on its ``raw``. Nothing else is modelled: a named operation that
reaches a worker op (``repo.get``, ``pr.create``, ...) hits ``_op`` and fails
loudly, exactly as :class:`tests.fakes.fake_github.FakeGithub` does.

``raw_lookup`` keeps the real contract (#558) on top of the stand-in's
``raw``: a status in ``missing`` is an answer (``None``), not a failure.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import GithubOps


class OpsStub(GithubOps):
    run_id = "stub"
    timeout_s = 0.0

    def __init__(self) -> None:
        # Deliberately no super().__init__: there is no worker client.
        pass

    def _op(self, op: str, params: dict[str, Any], *, timeout_s: float | None = None) -> Any:
        raise AssertionError(f"{type(self).__name__}: unexpected worker op {op!r} {params!r}")

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        raise AssertionError(f"{type(self).__name__}: unexpected raw call {method} {path}")

    def raw_lookup(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        missing: Sequence[int] = (404,),
    ) -> Any:
        try:
            # Positional ``body`` only when there is one: a stand-in's ``raw``
            # may take ``(method, path)`` alone.
            return self.raw(method, path) if body is None else self.raw(method, path, body)
        except GithubOpsError as exc:
            if exc.http_status in missing:
                return None
            raise
