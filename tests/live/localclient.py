"""A worker client that runs ``vcs.op`` jobs in this process (#1017).

The conformance suite's live backends drive a real backend object against
a live forge, but there is no sandbox and no worker process in a test
run. This client executes each job through the worker's own op registry
and REST transport (:mod:`sbxloop_worker.githubops`), the same code that
runs inside the github-role box — only the process boundary is missing.
The token is the caller's; the harness CA is trusted for the process
through ``SSL_CERT_FILE``, which the stdlib's default TLS context reads.
"""

from __future__ import annotations

import os
from pathlib import Path

from sbxloop_worker.githubops import GithubOpError, RestTransport, execute_op
from sbxloop_worker.protocol import ErrorInfo, JobRequest, JobResult, TransportSpec


class LocalWorkerClient:
    def __init__(self, spec: TransportSpec, token: str, *, ca_file: Path | None = None) -> None:
        self.spec = spec
        self._token = token
        if ca_file is not None:
            os.environ.setdefault("SSL_CERT_FILE", str(ca_file))
        self.jobs: list[JobRequest] = []

    def submit(self, job: JobRequest) -> JobResult:
        self.jobs.append(job)
        assert job.op is not None
        transport = RestTransport(self._token, spec=self.spec)
        params = {k: v for k, v in job.params.items() if k != "transport"}
        try:
            output = execute_op(job.op, params, transport=transport)
        except GithubOpError as exc:
            return JobResult(
                job_id=job.job_id,
                status="error",
                error=ErrorInfo(
                    type="GithubOpError", message=str(exc), http_status=exc.http_status
                ),
            )
        return JobResult(job_id=job.job_id, status="ok", output_json=output)
