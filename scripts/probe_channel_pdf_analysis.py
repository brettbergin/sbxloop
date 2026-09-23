"""CI: run the production PDF runner against a real disposable sbx VM."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from sbxloop.api.pdf_analysis import PdfAnalysisRunner


def _write_fixture(path: Path) -> None:
    writer = PdfWriter()
    for text in ("Introduction", "The answer on page two is 42"):
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
    with path.open("wb") as handle:
        writer.write(handle)


class Files:
    def __init__(self, path: Path) -> None:
        self.path = path

    def open_original(self, file_id: str, expected_size: int) -> BinaryIO:
        if file_id != "fin_" + "a" * 24 or self.path.stat().st_size != expected_size:
            raise ValueError("fixture changed")
        return self.path.open("rb")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sbxloop-pdf-probe-") as root:
        path = Path(root) / "fixture.pdf"
        _write_fixture(path)
        data = path.read_bytes()
        file = SimpleNamespace(
            id="fin_" + "a" * 24, size=len(data), sha256=hashlib.sha256(data).hexdigest()
        )
        result = json.loads(PdfAnalysisRunner(Files(path)).run(file))  # type: ignore[arg-type]
        if (
            result["total_pages"] != 2
            or result["pages"][1]["text"] != "The answer on page two is 42"
        ):
            raise AssertionError("isolated PDF extraction did not preserve page two")
    print("production PDF runner extracted page two inside the isolated VM")


if __name__ == "__main__":
    main()
