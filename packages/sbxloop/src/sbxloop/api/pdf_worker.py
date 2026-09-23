"""Text-only PDF parser executed inside a disposable, no-secret shell VM.

This module must never be imported to parse an upload in the API process.
The host copies it and the installed pure-Python pypdf package into the VM's
read-only input mount and validates its JSON output before persisting it.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

MAX_PDF_PAGES = 100
MAX_PAGE_TEXT = 8_000
MAX_TOTAL_TEXT = 240_000
MAX_PAGE_STREAM = 10_000_000


def extract_pdf(path: Path) -> dict[str, Any]:
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        return {"total_pages": 0, "pages": [], "complete": False, "reason": "encrypted"}
    total_pages = len(reader.pages)
    remaining = MAX_TOTAL_TEXT
    pages: list[dict[str, Any]] = []
    for index in range(min(total_pages, MAX_PDF_PAGES)):
        page = reader.pages[index]
        contents = page.get_contents()
        # pypdf documents that extraction can use far more memory than the
        # compressed input; the VM memory limit is the final backstop.
        if contents is not None and len(contents.get_data()) > MAX_PAGE_STREAM:
            pages.append({"page": index + 1, "text": "", "truncated": True, "reason": "large_page"})
            continue
        if remaining == 0:
            pages.append({"page": index + 1, "text": "", "truncated": True, "reason": "text_limit"})
            continue
        extracted = page.extract_text() or ""
        size = min(MAX_PAGE_TEXT, remaining)
        value = extracted[:size]
        remaining -= len(value)
        pages.append(
            {
                "page": index + 1,
                "text": value,
                "truncated": len(extracted) > len(value),
                "reason": "no_text" if not extracted else None,
            }
        )
    return {
        "total_pages": total_pages,
        "pages": pages,
        "complete": total_pages <= MAX_PDF_PAGES and all(not page["truncated"] for page in pages),
        "reason": "page_limit" if total_pages > MAX_PDF_PAGES else None,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(2)
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
    try:
        # Third-party parser diagnostics must never turn user-controlled PDF
        # bytes into unbounded sbx CLI output on the credentialed host.
        with (
            Path(os.devnull).open("w", encoding="utf-8") as sink,
            redirect_stdout(sink),
            redirect_stderr(sink),
        ):
            result = extract_pdf(Path(sys.argv[1]))
    except Exception:
        raise SystemExit(1) from None
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
