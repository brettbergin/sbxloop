"""PDF extraction produces bounded, page-addressable untrusted data."""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from sbxloop.api.pdf_analysis import ChannelPdfAnalysis, PdfAnalysisRunner, _validated_result
from sbxloop.api.pdf_worker import extract_pdf


def test_pdf_worker_preserves_page_numbers_and_limits_text(tmp_path: Path) -> None:
    pdf = tmp_path / "pages.pdf"
    writer = PdfWriter()
    for text in ("first page", "second page fact"):
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 100 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    with pdf.open("wb") as handle:
        writer.write(handle)

    result = extract_pdf(pdf)
    assert result["total_pages"] == 2
    assert result["pages"][1]["page"] == 2
    assert "second page fact" in result["pages"][1]["text"]


def test_runner_uses_isolated_profile_and_verifies_original() -> None:
    original = b"%PDF-1.7\nfixture"
    result = {
        "total_pages": 1,
        "pages": [{"page": 1, "text": "fact", "truncated": False, "reason": None}],
        "complete": True,
        "reason": None,
    }

    class Files:
        def open_original(self, file_id: str, expected_size: int) -> io.BytesIO:
            assert file_id == "fin_" + "a" * 24 and expected_size == len(original)
            return io.BytesIO(original)

    class CLI:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []
            self.removed: list[str] = []

        def ls(self) -> list[Any]:
            return []

        def run(self, *args: str, **kwargs: Any) -> Any:
            self.calls.append(args)
            return SimpleNamespace(stdout=json.dumps(result))

        def rm(self, name: str, **kwargs: Any) -> None:
            self.removed.append(name)

    cli = CLI()
    file = SimpleNamespace(
        id="fin_" + "a" * 24, size=len(original), sha256=hashlib.sha256(original).hexdigest()
    )
    parsed = json.loads(PdfAnalysisRunner(Files(), cli=cli).run(file))  # type: ignore[arg-type]
    assert parsed["pages"][0]["text"] == "fact"
    create, execute = cli.calls
    assert create[0] == "create" and create[6:8] == ("--deny-network", "**")
    assert create[8:10] == ("--skills", "off")
    assert any(value.endswith(":ro") for value in create)
    assert "OPENAI_API_KEY=" in create and "OPENAI_API_KEY=" in execute
    assert cli.removed == ["pdf-" + "a" * 24]


def test_result_validation_rejects_unbounded_or_unexpected_output() -> None:
    with pytest.raises(ValueError):
        _validated_result(
            json.dumps(
                {"total_pages": 1, "pages": [], "complete": True, "reason": None, "secret": "x"}
            )
        )
    with pytest.raises(ValueError):
        _validated_result(
            json.dumps({"total_pages": 2, "pages": [], "complete": True, "reason": None})
        )
    with pytest.raises(ValueError):
        _validated_result(
            json.dumps(
                {
                    "total_pages": 1,
                    "pages": [{"page": 1, "text": "x" * 8_001, "truncated": False, "reason": None}],
                    "complete": True,
                    "reason": None,
                }
            )
        )


def test_daemon_close_does_not_wait_for_a_slow_sandbox() -> None:
    started = threading.Event()
    release = threading.Event()
    finished: list[str] = []

    class Files:
        def claim_pdf(self, file_id: str) -> Any:
            return SimpleNamespace(id=file_id, sha256="abc")

        def finish_pdf(self, file_id: str, sha256: str, result: str | None) -> None:
            finished.append(file_id)

    class Runner:
        def run(self, file: Any) -> str:
            started.set()
            assert release.wait(5)
            return "{}"

    analysis = ChannelPdfAnalysis(Files(), runner=Runner())  # type: ignore[arg-type]
    analysis.submit("fin_" + "a" * 24)
    assert started.wait(1)
    before = time.monotonic()
    analysis.close()
    assert time.monotonic() - before < 0.1
    release.set()
    analysis._thread.join(timeout=1)
    assert not finished
