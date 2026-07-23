"""Shared fixtures: the fake sbx CLI harness."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

FAKE_SBX_SOURCE = Path(__file__).parent / "fakes" / "fake_sbx.py"


class FakeSbx:
    """Handle for asserting against (and scripting) the fake sbx CLI."""

    def __init__(self, binary: Path, state: Path) -> None:
        self.binary = binary
        self.state = state

    # -- assertions --------------------------------------------------------

    def invocations(self, command: str | None = None) -> list[list[str]]:
        path = self.state / "invocations.jsonl"
        if not path.is_file():
            return []
        calls = [json.loads(line)["args"] for line in path.read_text().splitlines()]
        if command is not None:
            calls = [c for c in calls if c[: len(command.split())] == command.split()]
        return calls

    def raw_invocations(self) -> list[dict[str, Any]]:
        path = self.state / "invocations.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def sandbox_fs(self, name: str) -> Path:
        return self.state / "sandboxes" / name / "fs"

    def meta(self, name: str) -> dict[str, Any]:
        return json.loads((self.state / "sandboxes" / name / "meta.json").read_text())

    def policies(self) -> list[list[str]]:
        path = self.state / "policies.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line)["args"] for line in path.read_text().splitlines()]

    def secrets(self) -> list[dict[str, Any]]:
        path = self.state / "secrets.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    # -- scripting ---------------------------------------------------------

    def script(
        self,
        prefix: str,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        once: bool = False,
    ) -> None:
        path = self.state / "responses.json"
        responses = json.loads(path.read_text()) if path.is_file() else []
        responses.append(
            {
                "prefix": prefix,
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "once": once,
            }
        )
        path.write_text(json.dumps(responses))

    def fail_next(self, prefix: str, *, returncode: int = 1, stderr: str = "error") -> None:
        self.script(prefix, returncode=returncode, stderr=stderr, once=True)


@pytest.fixture
def fake_sbx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSbx:
    state = tmp_path / "sbx-state"
    state.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "sbx"
    shim.write_text(
        f'#!/bin/sh\nSBX_FAKE_DIR="{state}" exec "{sys.executable}" "{FAKE_SBX_SOURCE}" "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bin_dir), prepend=":")
    return FakeSbx(binary=shim, state=state)
