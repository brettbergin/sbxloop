"""Durable, isolated PDF text analysis for channel uploads."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import queue
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from sbxloop.api.channel_files import ChannelFileStore, ChannelInputFile
from sbxloop.log import get_logger
from sbxloop.sbx.cli import SbxCLI

log = get_logger(__name__)

ANALYSIS_APP = "sbxloop-analysis"
MAX_RESULT_BYTES = 2_000_000
_CLEAR_CREDENTIALS = (
    "-e",
    "GH_TOKEN=",
    "-e",
    "GITHUB_TOKEN=",
    "-e",
    "DOCKERHUB_TOKEN=",
    "-e",
    "OPENAI_API_KEY=",
    "-e",
    "ANTHROPIC_API_KEY=",
    "-e",
    "COPILOT_GITHUB_TOKEN=",
)


def _validated_result(raw: str) -> str:
    """Accept only the worker's bounded page schema, never arbitrary output."""
    if len(raw.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError("PDF analysis output exceeds limit")
    value: Any = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"total_pages", "pages", "complete", "reason"}:
        raise ValueError("invalid PDF analysis result")
    if (
        not isinstance(value["total_pages"], int)
        or isinstance(value["total_pages"], bool)
        or not 0 <= value["total_pages"] <= 1_000_000
    ):
        raise ValueError("invalid PDF page count")
    if (
        not isinstance(value["complete"], bool)
        or not isinstance(value["pages"], list)
        or len(value["pages"]) > 100
    ):
        raise ValueError("invalid PDF page listing")
    if value["reason"] not in (None, "encrypted", "page_limit"):
        raise ValueError("invalid PDF summary reason")
    total_text = 0
    for number, page in enumerate(value["pages"], start=1):
        if not isinstance(page, dict) or set(page) != {"page", "text", "truncated", "reason"}:
            raise ValueError("invalid PDF page result")
        if page["page"] != number or not isinstance(page["text"], str) or len(page["text"]) > 8_000:
            raise ValueError("invalid PDF page text")
        if not isinstance(page["truncated"], bool) or page["reason"] not in (
            None,
            "no_text",
            "large_page",
            "text_limit",
        ):
            raise ValueError("invalid PDF page limit")
        total_text += len(page["text"])
    if total_text > 240_000:
        raise ValueError("PDF text exceeds limit")
    if len(value["pages"]) != min(value["total_pages"], 100):
        raise ValueError("PDF page count does not match extracted pages")
    if value["reason"] == "encrypted" and value["total_pages"] != 0:
        raise ValueError("invalid encrypted PDF result")
    if value["reason"] == "page_limit" and value["total_pages"] <= 100:
        raise ValueError("invalid PDF page limit")
    if value["complete"] != (
        value["reason"] is None and all(not page["truncated"] for page in value["pages"])
    ):
        raise ValueError("inconsistent PDF completion status")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class PdfAnalysisRunner:
    """Copies one verified original into the same VM profile proven in CI."""

    def __init__(self, files: ChannelFileStore, cli: SbxCLI | None = None) -> None:
        self.files = files
        self.cli = cli or SbxCLI(app_name=ANALYSIS_APP)

    def run(self, file: ChannelInputFile) -> str:
        if file.size is None or file.sha256 is None:
            raise ValueError("PDF original is incomplete")
        name = "pdf-" + file.id[4:]
        with tempfile.TemporaryDirectory(prefix="sbxloop-pdf-") as root_name:
            root = Path(root_name)
            work = root / "work"
            inputs = root / "input"
            work.mkdir()
            inputs.mkdir()
            original = inputs / "original.pdf"
            digest = hashlib.sha256()
            with (
                self.files.open_original(file.id, file.size) as source,
                original.open("wb") as target,
            ):
                for chunk in iter(lambda: source.read(1 << 20), b""):
                    digest.update(chunk)
                    target.write(chunk)
            if digest.hexdigest() != file.sha256:
                raise ValueError("PDF original checksum changed")
            source_package = importlib.util.find_spec("pypdf")
            if source_package is None or not source_package.submodule_search_locations:
                raise RuntimeError("pypdf is not installed")
            shutil.copytree(
                Path(next(iter(source_package.submodule_search_locations))),
                inputs / "pypdf",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            shutil.copyfile(Path(__file__).with_name("pdf_worker.py"), inputs / "pdf_worker.py")
            # A previous daemon may have stopped while this stable name was
            # live. Settle its removal before reusing the name.
            if any(box.name == name for box in self.cli.ls()):
                self.cli.rm(name)
            try:
                self.cli.run(
                    "create",
                    f"--name={name}",
                    "--cpus",
                    "1",
                    "--memory",
                    "512m",
                    "--deny-network",
                    "**",
                    "--skills",
                    "off",
                    *_CLEAR_CREDENTIALS,
                    "shell",
                    str(work),
                    f"{inputs}:ro",
                    timeout=600,
                )
                output = self.cli.run(
                    "exec",
                    *_CLEAR_CREDENTIALS,
                    name,
                    "python3",
                    str(inputs / "pdf_worker.py"),
                    str(original),
                    timeout=45,
                ).stdout
                return _validated_result(output.strip())
            finally:
                # A timed-out or crashed parser must not leave a credentialless
                # VM alive after its job. Recovery also removes a stale name.
                self.cli.rm(name)


class ChannelPdfAnalysis:
    def __init__(self, files: ChannelFileStore, *, runner: PdfAnalysisRunner | None = None) -> None:
        self.files = files
        self.runner = runner or PdfAnalysisRunner(files)
        self._jobs: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._submitted: set[str] = set()
        self._stopping = False
        # A daemon shutdown must not wait for an sbx create's 600-second
        # deadline. An interrupted VM is removed by the next startup's
        # deterministic-name recovery before that file is retried.
        self._thread = threading.Thread(target=self._work, name="sbxloop-pdf-analysis", daemon=True)
        self._thread.start()

    def submit(self, file_id: str) -> None:
        with self._lock:
            if self._stopping or file_id in self._submitted:
                return
            self._submitted.add(file_id)
            self._jobs.put(file_id)

    def _work(self) -> None:
        while (file_id := self._jobs.get()) is not None:
            if self._stopping:
                return
            try:
                self._process(file_id)
            except Exception as exc:
                log.warning(
                    "channel.pdf_job_store_failed", file_id=file_id, error=type(exc).__name__
                )

    def recover(self) -> None:
        for file_id in self.files.pending_pdf_ids():
            self.submit(file_id)

    def _process(self, file_id: str) -> None:
        try:
            file = self.files.claim_pdf(file_id)
            if file is None or file.sha256 is None:
                return
            try:
                result = self.runner.run(file)
            except Exception as exc:
                log.warning(
                    "channel.pdf_analysis_failed", file_id=file_id, error=type(exc).__name__
                )
                result = None
            if not self._stopping:
                self.files.finish_pdf(file_id, file.sha256, result)
        finally:
            with self._lock:
                self._submitted.discard(file_id)

    def close(self) -> None:
        with self._lock:
            self._stopping = True
            self._jobs.put(None)
