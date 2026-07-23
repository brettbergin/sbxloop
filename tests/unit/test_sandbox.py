"""Sandbox handle tests against the fake sbx harness."""

from pathlib import Path

import pytest

from sdxloop.sbx.cli import SbxCLI
from sdxloop.sbx.models import SandboxSpec
from sdxloop.sbx.sandbox import Sandbox
from tests.conftest import FakeSbx


@pytest.fixture
def sandbox(fake_sbx: FakeSbx, tmp_path: Path) -> Sandbox:
    cli = SbxCLI(binary=str(fake_sbx.binary))
    cli.create(SandboxSpec(name="boxa", role="agent", workspace=tmp_path))
    return Sandbox(cli, "boxa")


def test_write_and_read_text(sandbox: Sandbox) -> None:
    sandbox.write_text("/home/agent/.sdxloop/jobs/j1.json", '{"job": 1}')
    assert sandbox.read_text("/home/agent/.sdxloop/jobs/j1.json") == '{"job": 1}'


def test_mkdirs_and_exec(sandbox: Sandbox, fake_sbx: FakeSbx) -> None:
    sandbox.mkdirs("/home/agent/.sdxloop/a", "/home/agent/.sdxloop/b")
    fs = fake_sbx.sandbox_fs("boxa")
    assert (fs / "home/agent/.sdxloop/a").is_dir()
    assert (fs / "home/agent/.sdxloop/b").is_dir()
    result = sandbox.exec(["sh", "-c", "echo -n out"])
    assert result.ok


def test_cp_in_out(sandbox: Sandbox, tmp_path: Path) -> None:
    payload = tmp_path / "in.txt"
    payload.write_text("data")
    sandbox.cp_in(payload, "/home/agent/in.txt")
    out = tmp_path / "out.txt"
    sandbox.cp_out("/home/agent/in.txt", out)
    assert out.read_text() == "data"


def test_exec_stream_lines(sandbox: Sandbox) -> None:
    proc = sandbox.exec_stream(["sh", "-c", "echo one; echo two"])
    assert proc.stdout is not None
    lines = [line.strip() for line in proc.stdout]
    proc.wait()
    assert lines == ["one", "two"]
    assert proc.returncode == 0


def test_stop_and_rm(sandbox: Sandbox, fake_sbx: FakeSbx) -> None:
    sandbox.stop()
    assert fake_sbx.meta("boxa")["status"] == "stopped"
    sandbox.rm()
    assert not (fake_sbx.state / "sandboxes" / "boxa").exists()


def test_repr(sandbox: Sandbox) -> None:
    assert repr(sandbox) == "Sandbox('boxa')"
