"""Worker-side event emission: JSONL file (durable) + stdout mirror (live)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from sbxloop_worker.protocol import Event


class EventWriter:
    """Appends events to a JSONL file (fsync per line) and mirrors to stdout.

    The file is the durable record the host can always recover with ``cp``;
    the stdout mirror is the live telemetry channel when the host streams
    ``sbx exec`` output. Either channel may be lost independently — the
    result file, not events, is the source of truth for job outcomes.
    """

    def __init__(
        self,
        path: Path,
        run_id: str,
        job_id: str | None = None,
        mirror: TextIO | None = None,
    ) -> None:
        self.run_id = run_id
        self.job_id = job_id
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file: TextIO | None = path.open("a", encoding="utf-8")
        self._mirror = sys.stdout if mirror is None else mirror

    def emit(self, type: str, **data: Any) -> Event:
        event = Event.now(type, self.run_id, job_id=self.job_id, **data)
        line = event.to_json_line()
        if self._file is not None:
            self._file.write(line + "\n")
            self._file.flush()
            os.fsync(self._file.fileno())
        if self._mirror is not None:
            print(line, file=self._mirror, flush=True)
        return event

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> EventWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
