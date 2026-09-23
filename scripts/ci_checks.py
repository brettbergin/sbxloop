"""Fail-closed verdicts shared by CI's required status checks."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def dependencies(needs: dict, required: list[str]) -> None:
    failed = [name for name in required if needs.get(name, {}).get("result") != "success"]
    if failed:
        raise ValueError(f"Required jobs did not succeed: {', '.join(failed)}")


def coverage_files(directory: Path, slices: list[str]) -> None:
    expected = {f".coverage.{name}" for name in slices}
    actual = {path.name for path in directory.glob(".coverage.*")}
    missing = expected - actual
    unexpected = actual - expected
    empty = {name for name in expected & actual if (directory / name).stat().st_size == 0}
    if missing or unexpected or empty:
        raise ValueError(
            f"Incomplete coverage: missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}, empty={sorted(empty)}"
        )


if __name__ == "__main__":
    try:
        if sys.argv[1] == "dependencies":
            dependencies(json.loads(os.environ["NEEDS_JSON"]), sys.argv[2:])
        elif sys.argv[1] == "coverage":
            coverage_files(Path(), sys.argv[2:])
        else:
            raise ValueError("unknown CI verdict")
    except (ValueError, KeyError, OSError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
