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
        """Recorded policy mutations, one entry per rule.

        sbx takes RESOURCES as a comma-separated list, so a batched
        ``allow network a.com,b.com`` is expanded here into per-domain
        entries — assertions name single domains either way. Raw argv
        (batching included) is visible via ``invocations("policy")``.
        """
        path = self.state / "policies.jsonl"
        if not path.is_file():
            return []
        entries = []
        for line in path.read_text().splitlines():
            args = json.loads(line)["args"]
            if len(args) >= 3 and args[1] == "network":
                for resource in args[2].split(","):
                    entries.append([*args[:2], resource, *args[3:]])
            else:
                entries.append(args)
        return entries

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
    # The fake execs on the host, so a real token in the operator's own
    # environment would leak into the "sandbox" and make the secret-env
    # visibility probe answer the opposite of field-observed sbx behavior.
    # Tests that want the env visible set it explicitly.
    for leaked in ("GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
        monkeypatch.delenv(leaked, raising=False)
    return FakeSbx(binary=shim, state=state)


@pytest.fixture(autouse=True, scope="session")
def _structlog_through_stdlib() -> None:
    """Route structlog through ``logging`` for the whole session so ``caplog``
    captures every record with its structured fields rendered. Tests that
    assert on a level below WARNING should say so with ``caplog.at_level``:
    CLI tests reconfigure the root level as the real entrypoint does."""
    from sbxloop.log import configure_logging

    configure_logging("DEBUG")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at the test's tmp dir.

    ``state_dir`` defaults to ``~/.sbxloop`` and config discovery reads
    ``~/.config/sbxloop/sbxloop.toml``, so without this every CLI test that
    relies on defaults would read (and write!) the operator's real state.
    Tests that chdir into ``tmp_path`` get ``tmp_path/.sbxloop`` as the
    default state dir — the same place the old relative default landed.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() reads USERPROFILE on Windows, so pin it too.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path
