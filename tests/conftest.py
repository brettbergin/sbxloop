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
        passthrough: bool = False,
    ) -> None:
        """Script the fake's answer to every invocation starting with ``prefix``.

        First match wins, in scripting order. ``passthrough`` reserves a
        prefix for the fake's real behaviour so a broader prefix scripted
        after it (say, every ``sh -c``) does not swallow it — the workspace
        mount probe is the usual one to protect.
        """
        path = self.state / "responses.json"
        responses = json.loads(path.read_text()) if path.is_file() else []
        responses.append(
            {
                "prefix": prefix,
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "once": once,
                "passthrough": passthrough,
            }
        )
        path.write_text(json.dumps(responses))

    def fail_next(self, prefix: str, *, returncode: int = 1, stderr: str = "error") -> None:
        self.script(prefix, returncode=returncode, stderr=stderr, once=True)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-trail",
        action="store_true",
        default=False,
        help="re-record tests/fixtures/code_run_trail/*.json from the current code "
        "(tests/unit/test_code_run_trail.py); a deliberate act, read the diff",
    )


@pytest.fixture
def fake_sbx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSbx:
    state = tmp_path / "sbx-state"
    state.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "sbx"
    # ``-I -S`` skips site-packages, ``.pth`` files and the user site: the fake
    # is stdlib-only, and the suite execs it thousands of times, so interpreter
    # start-up is a measurable share of wall clock (#750).
    shim.write_text(
        "#!/bin/sh\n"
        f'SBX_FAKE_DIR="{state}" exec "{sys.executable}" -I -S "{FAKE_SBX_SOURCE}" "$@"\n'
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


@pytest.fixture(autouse=True, scope="session")
def _cli_console_follows_columns() -> None:
    """Make the CLI's module-level rich ``Console`` read ``COLUMNS``/``LINES``
    at render time, the way CLI tests assume when they ``monkeypatch.setenv``
    them or pass ``env=`` to ``CliRunner.invoke``.

    rich freezes both variables into the console when they are set at
    construction, and construction happens at import. Importing GNU readline
    (which pytest's startup does) ``setenv``s ``COLUMNS=80``/``LINES=24`` in
    the C environment; the importing process never sees that in ``os.environ``
    but every xdist worker inherits it, so on a Linux system Python the
    console is born frozen at 80 columns and rich tables truncate cells the
    tests look for. libedit builds (macOS, uv-managed CPython) do not, which
    is why the failure only shows on distro interpreters.
    """
    from sbxloop.cli import app as app_module

    console = app_module.console
    # These are rich internals; fail loudly if a rich upgrade renames them
    # rather than quietly bringing the frozen-width failure back.
    assert hasattr(console, "_width") and hasattr(console, "_height")
    console._width = None
    console._height = None


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at the test's tmp dir, so the sbxloop home defaults to
    ``tmp_path/.sbxloop`` and no test reads (or writes!) the operator's
    real home. ``SBXLOOP_HOME`` is cleared for the same reason: a developer
    shell that exports it must not redirect the suite."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() reads USERPROFILE on Windows, so pin it too.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("SBXLOOP_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    # Lay the home out the way `sbxloop init` leaves it, so commands that
    # check the layout (doctor) see an initialised host; a test about an
    # uninitialised host removes the record itself.
    from sbxloop.paths import SbxloopHome

    home = SbxloopHome(tmp_path / ".sbxloop")
    home.ensure_tree()
    home.write_record(sbxloop_version="test", created_by="tests/conftest.py")
    return tmp_path
