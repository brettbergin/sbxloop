from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from sbxloop_worker.protocol import Event, JobRequest, JobResult


class WorkerHarness:
    """Runs the real worker as a subprocess (echo backend) and parses outputs."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.events_path = tmp_path / "events.jsonl"
        self.result_path = tmp_path / "result.json"
        self.job_path = tmp_path / "job.json"

    def run(
        self,
        job: JobRequest,
        *,
        heartbeat: float = 0.0,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> subprocess.CompletedProcess[str]:
        self.job_path.write_text(job.model_dump_json())
        merged = dict(os.environ)
        merged["SBXLOOP_WORKER_BACKEND"] = "echo"
        merged.update(env or {})
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "sbxloop_worker",
                "run",
                "--job",
                str(self.job_path),
                "--events",
                str(self.events_path),
                "--result",
                str(self.result_path),
                "--heartbeat",
                str(heartbeat),
                "--env-file",
                str(self.tmp_path / "env.sh"),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=merged,
        )

    def events(self) -> list[Event]:
        if not self.events_path.is_file():
            return []
        return [
            Event.from_json_line(line)
            for line in self.events_path.read_text().splitlines()
            if line.strip()
        ]

    def result(self) -> JobResult:
        return JobResult.model_validate_json(self.result_path.read_text())

    def write_script(self, responses: list[dict[str, Any]]) -> Path:
        path = self.tmp_path / "echo-script.json"
        path.write_text(json.dumps(responses))
        return path
