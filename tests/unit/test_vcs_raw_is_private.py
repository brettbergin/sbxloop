"""The generic transport is private to the backend package.

``GithubOps.raw`` / ``raw_lookup`` and the ``raw_pages`` walker let a caller
spell a forge path by hand. Fifty such sites had accumulated outside the
GitHub package before this gate: GitHub knowledge living where nothing
types it and nothing tests it as an operation. Every one of them is a
named operation on a role in :mod:`sbxloop.vcs.protocol` now, and a new
one must be too — a path a module outside ``vcs/`` needs is an operation a
role lacks.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "packages" / "sbxloop" / "src" / "sbxloop"
BACKENDS = SRC / "vcs"

# ``<anything>.raw(``, ``raw_lookup(<ops>,`` and ``raw_pages(<ops>,`` — the
# three spellings a caller has used to hand a path to the transport.
RAW_CALL = re.compile(r"\.raw\(|\braw_lookup\(|\braw_pages\(")


def test_no_module_outside_the_backend_packages_spells_a_forge_path() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if BACKENDS in path.parents:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if RAW_CALL.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)
