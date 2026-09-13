"""What every worker-backed backend shares: one ``vcs.op`` job per
operation, and the generic transport behind the named ones.

A backend under ``vcs/<name>/`` never talks to its forge from the host.
Each operation becomes a :class:`~sbxloop_worker.protocol.JobRequest` of
kind ``vcs.op`` submitted to the sandbox that holds the credential, with
the transport descriptor (#1015) telling the worker how that forge is
spoken to. The methods here are the transport: :meth:`raw` for one JSON
call, :meth:`raw_lookup` for a call whose "no" is an answer (#558),
:meth:`raw_text` for an endpoint that answers text, and :meth:`raw_pages`
for a list walked by page number. They are private to the backend
package (``tests/unit/test_vcs_raw_is_private.py``); a path a consumer
needs is a named operation on a role.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from sbxloop.errors import GithubOpsError
from sbxloop.ids import new_job_id
from sbxloop.log import get_logger
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import JobRequest, TransportSpec

log = get_logger(__name__)

# Every forge's list endpoints page at 100 at most; a walk that will not
# reach the end in this many full pages is refused rather than cut (#614).
PAGE_SIZE = 100
MAX_PAGES = 10


class PaginationError(GithubOpsError):
    """A list longer than the reader will follow. The read is incomplete
    and must be treated as unread, never as "what we saw is all there
    is"."""


class MalformedResponse(GithubOpsError):
    """The forge answered, but not in the shape the operation is defined
    to return. Never a miss and never a refusal: those carry a status."""

    def __init__(self, what: str, data: Any) -> None:
        super().__init__(f"{what} returned a malformed result: {data!r}")
        self.data = data


class JobBackend:
    """The worker-job plumbing a backend class builds on."""

    #: The forge kind this backend answers for; the loop's ``[vcs] kind``.
    KIND: str = ""
    #: How the forge is named in an error message, so a chronology reader
    #: can tell which forge refused.
    LABEL: str = "forge"

    def __init__(
        self,
        client: WorkerClient,
        run_id: str,
        *,
        timeout_s: float = 120.0,
        transport: TransportSpec | None = None,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.timeout_s = timeout_s
        # How the worker reaches the forge (#1015); None sends no
        # descriptor and the worker serves the job as GitHub.
        self.transport = transport

    def _op(self, op: str, params: dict[str, Any], *, timeout_s: float | None = None) -> Any:
        if self.transport is not None:
            params = {**params, "transport": self.transport.model_dump(mode="json")}
        job = JobRequest(
            job_id=new_job_id(),
            run_id=self.run_id,
            kind="vcs.op",
            op=op,
            params=params,
            timeout_s=timeout_s if timeout_s is not None else self.timeout_s,
        )
        started = time.monotonic()
        result = self.client.submit(job)
        error = result.error
        log.debug(
            "vcs.op",
            run=self.run_id,
            kind=self.KIND,
            job=job.job_id,
            op=op,
            repo=params.get("repo"),
            status=result.status,
            http_status=error.http_status if error is not None else None,
            duration_s=round(time.monotonic() - started, 2),
        )
        if result.status != "ok":
            assert result.error is not None
            raise GithubOpsError(
                f"{self.LABEL} op {op} failed: {result.error.type}: {result.error.message}",
                http_status=result.error.http_status,
            )
        return result.output_json

    # -- the generic transport ------------------------------------------------

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        params: dict[str, Any] = {"method": method, "path": path}
        if body is not None:
            params["body"] = body
        return self._op("raw.api", params)

    def raw_lookup(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        missing: Sequence[int] = (404,),
    ) -> Any:
        """A call whose "no" is an answer (#558): a status in ``missing``
        comes back as ``None`` and never as a failed worker job."""
        params: dict[str, Any] = {
            "method": method,
            "path": path,
            "allow_missing_statuses": [int(s) for s in missing],
        }
        if body is not None:
            params["body"] = body
        try:
            data = self._op("raw.api", params)
        except GithubOpsError as exc:
            if exc.http_status in missing:
                return None
            raise
        if isinstance(data, dict) and data.get("missing") is True:
            return None
        return data

    def raw_text(self, method: str, path: str, *, missing: Sequence[int] = ()) -> str | None:
        """A call whose answer is a text body (a job trace); ``None`` for a
        status in ``missing``."""
        params: dict[str, Any] = {"method": method, "path": path}
        if missing:
            params["allow_missing_statuses"] = [int(s) for s in missing]
        try:
            data = self._op("raw.text", params)
        except GithubOpsError as exc:
            if exc.http_status in missing:
                return None
            raise
        if isinstance(data, dict) and data.get("missing") is True:
            return None
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str):
            raise MalformedResponse(f"{method} {path}", data)
        return text

    def raw_pages(self, path: str) -> list[Any]:
        """Every entry of a list endpoint, following ``page=`` until a short
        page; refused past :data:`MAX_PAGES` full pages."""
        sep = "&" if "?" in path else "?"
        rows: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            data = self.raw("GET", f"{path}{sep}per_page={PAGE_SIZE}&page={page}")
            if not isinstance(data, list):
                return rows
            rows.extend(data)
            if len(data) < PAGE_SIZE:
                return rows
        raise PaginationError(
            f"GET {path} has more than {MAX_PAGES * PAGE_SIZE} entries; "
            "the list was not read to its end"
        )

    @staticmethod
    def _dict(what: str, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise MalformedResponse(what, data)
        return data

    @staticmethod
    def _list(what: str, data: Any) -> list[Any]:
        if not isinstance(data, list):
            raise MalformedResponse(what, data)
        return data
