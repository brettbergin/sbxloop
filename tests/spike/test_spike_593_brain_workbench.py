"""Spike #593: a brain-box session working a credential-less workbench.

Runs the REAL worker process (echo backend) in a "brain" sandbox through the
fake sbx, with every file/shell operation brokered by the host to a second
"workbench" sandbox — the concierge's ``host_tools`` mechanism generalized to
the builder. The questions the spike answers:

1. **Mechanics** — can a session in box A mutate a tree in box B with the
   host mediating every call? (test_brain_session_builds_in_workbench)
2. **Security** — does the brain's credential stay out of the workbench, and
   does the host checkpoint actually refuse out-of-policy operations?
   (test_credential_never_reaches_workbench, test_paths_are_confined)
3. **Cost** — what does one brokered call cost, structurally (host-side sbx
   invocations per call) and in wall time on this host?
   (test_brokered_call_cost_profile — prints the profile under ``-s``)

Fake-sbx wall times are NOT microVM wall times; the transferable numbers are
the per-call *sbx invocation count* and the worker's 250ms response-poll
floor (``sbxloop_worker.hosttools.POLL_INTERVAL_S``).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from sbxloop.events import EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxSpec
from sbxloop.sbx.sandbox import WORK_DIR, Sandbox
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import JobRequest
from tests.conftest import FakeSbx
from tests.spike.workbench import WorkbenchTools

BRAIN = "spike-brain"
WORKBENCH = "spike-workbench"


@pytest.fixture(autouse=True)
def echo_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBXLOOP_WORKER_BACKEND", "echo")


@pytest.fixture
def cli(fake_sbx: FakeSbx) -> SbxCLI:
    return SbxCLI(binary=str(fake_sbx.binary))


@pytest.fixture
def brain(cli: SbxCLI, tmp_path: Path) -> Sandbox:
    cli.create(SandboxSpec(name=BRAIN, role="agent", workspace=tmp_path / "brain-ws"))
    return Sandbox(cli, BRAIN)


@pytest.fixture
def workbench(cli: SbxCLI, tmp_path: Path) -> Sandbox:
    cli.create(SandboxSpec(name=WORKBENCH, role="agent", workspace=tmp_path / "wb-ws"))
    sandbox = Sandbox(cli, WORKBENCH)
    sandbox.mkdirs(WORK_DIR)
    return sandbox


def script_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict],
    text: str = "spike",
    name: str = "echo-script",
) -> None:
    # One file per session: the echo backend tracks its cursor in a sidecar
    # next to the script, so reusing a path would read a stale cursor.
    script = tmp_path / f"{name}.json"
    script.write_text(json.dumps([{"text": text, "host_tool_calls": calls}]))
    monkeypatch.setenv("SBXLOOP_ECHO_SCRIPT", str(script))


def brain_client(brain: Sandbox, **kwargs: object) -> WorkerClient:
    kwargs.setdefault("python", sys.executable)
    return WorkerClient(brain, EventBus(), **kwargs)  # type: ignore[arg-type]


def session_job(tools: WorkbenchTools, job_id: str = "spike1") -> JobRequest:
    return JobRequest(
        job_id=job_id,
        run_id="rspike",
        kind="agent.session",
        prompt="build in the workbench",
        host_tools=tools.specs(),
    )


class TestTopology:
    def test_brain_session_builds_in_workbench(
        self,
        brain: Sandbox,
        workbench: Sandbox,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A session in the brain box writes, runs, and reads back code that
        exists only in the workbench box — every step host-mediated."""
        tools = WorkbenchTools(workbench)
        script_session(
            tmp_path,
            monkeypatch,
            [
                {
                    "name": "wb_write",
                    "arguments": {"path": "pkg/hello.py", "content": "print('hi')\n"},
                },
                {"name": "wb_shell", "arguments": {"cmd": "wc -l < pkg/hello.py"}},
                {"name": "wb_read", "arguments": {"path": "pkg/hello.py"}},
                {"name": "wb_list", "arguments": {}},
            ],
        )
        result = brain_client(brain).submit(session_job(tools), tool_handler=tools.handle)

        assert result.status == "ok"
        assert result.output_text is not None
        assert "wrote pkg/hello.py" in result.output_text
        assert "exit=0" in result.output_text
        assert "print('hi')" in result.output_text
        assert "./pkg/hello.py" in result.output_text

        # The tree exists in the workbench and ONLY in the workbench.
        wb_file = fake_sbx.sandbox_fs(WORKBENCH) / "home/agent/work/pkg/hello.py"
        assert wb_file.is_file() and wb_file.read_text() == "print('hi')\n"
        assert not list(fake_sbx.sandbox_fs(BRAIN).rglob("hello.py"))

        # The host saw (and could have refused) every operation.
        assert [entry.tool for entry in tools.audit] == [
            "wb_write",
            "wb_shell",
            "wb_read",
            "wb_list",
        ]
        assert all(entry.ok for entry in tools.audit)

    def test_credential_never_reaches_workbench(
        self,
        brain: Sandbox,
        workbench: Sandbox,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The brain's per-job stdin credential (#592) is visible to the
        session's process and invisible to everything the workbench runs —
        the property #593 exists to establish for BYO API keys."""
        monkeypatch.setenv("SBX_FAKE_EXEC_STDIN", "1")
        secret = "sk-spike-inference-credential"
        tools = WorkbenchTools(workbench)
        script_session(
            tmp_path,
            monkeypatch,
            [
                {
                    "name": "wb_shell",
                    "arguments": {"cmd": "printenv SPIKE_MODEL_KEY; echo probed=$?"},
                }
            ],
        )
        client = brain_client(brain, job_env=lambda: {"SPIKE_MODEL_KEY": secret})
        result = client.submit(session_job(tools, "spike-cred"), tool_handler=tools.handle)

        assert result.status == "ok"
        assert result.output_text is not None
        # The workbench probe came back empty-handed: printenv found nothing.
        assert "probed=1" in result.output_text
        assert secret not in result.output_text

    def test_paths_are_confined(
        self,
        brain: Sandbox,
        workbench: Sandbox,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The host checkpoint refuses traversal, absolute paths, and .git
        writes — the session reads the refusal and carries on."""
        tools = WorkbenchTools(workbench)
        script_session(
            tmp_path,
            monkeypatch,
            [
                {"name": "wb_read", "arguments": {"path": "../../.sbxloop/env.sh"}},
                {"name": "wb_read", "arguments": {"path": "/etc/passwd"}},
                {
                    "name": "wb_write",
                    "arguments": {"path": ".git/hooks/pre-commit", "content": "x"},
                },
            ],
        )
        result = brain_client(brain).submit(
            session_job(tools, "spike-conf"), tool_handler=tools.handle
        )

        assert result.status == "ok"
        assert result.output_text is not None
        assert "path traversal is refused" in result.output_text
        assert "absolute paths are refused" in result.output_text
        assert "writes under .git/ are refused" in result.output_text
        assert [entry.ok for entry in tools.audit] == [False, False, False]


class TestCostProfile:
    N_CALLS = 8

    def _run_session(
        self,
        brain: Sandbox,
        tools: WorkbenchTools,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        calls: list[dict],
        job_id: str,
    ) -> float:
        script_session(tmp_path, monkeypatch, calls, name=job_id)
        started = time.monotonic()
        result = brain_client(brain).submit(session_job(tools, job_id), tool_handler=tools.handle)
        elapsed = time.monotonic() - started
        assert result.status == "ok"
        return elapsed

    def test_brokered_call_cost_profile(
        self,
        brain: Sandbox,
        workbench: Sandbox,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Structural cost per brokered call, plus a wall-time profile.

        Asserted: each wb_shell call costs exactly 2 host-side sbx
        invocations (the workbench exec + the response cp) on top of the
        session's own fixed launch cost. Printed under ``-s``: per-call wall
        time — dominated on any host by the worker's 250ms response poll.
        """
        tools = WorkbenchTools(workbench)
        shell_call = {"name": "wb_shell", "arguments": {"cmd": "true"}}

        baseline_invocations = None
        # Session with zero brokered calls: the fixed launch cost.
        base_s = self._run_session(brain, tools, tmp_path, monkeypatch, [], "spike-base")
        baseline_invocations = len(fake_sbx.invocations())

        loaded_s = self._run_session(
            brain, tools, tmp_path, monkeypatch, [shell_call] * self.N_CALLS, "spike-load"
        )
        loaded_invocations = len(fake_sbx.invocations())

        per_call_invocations = (loaded_invocations - baseline_invocations) / self.N_CALLS
        # exec (workbench command) + cp (response file); the job-file write
        # and worker launch are the session's fixed cost, not the call's.
        assert per_call_invocations == pytest.approx(2.0, abs=0.5)

        per_call_s = (loaded_s - base_s) / self.N_CALLS
        handler_s = sum(e.duration_s for e in tools.audit) / max(len(tools.audit), 1)
        print(
            f"\n[spike-593] fixed session cost: {base_s:.2f}s; "
            f"{self.N_CALLS} brokered calls: +{loaded_s - base_s:.2f}s "
            f"({per_call_s * 1000:.0f}ms/call wall, "
            f"{handler_s * 1000:.0f}ms/call handler, "
            f"{per_call_invocations:.1f} sbx invocations/call; "
            f"worker response-poll floor 250ms)"
        )
        # Sanity bound only — absolute times vary by host; the poll floor
        # guarantees a brokered call cannot be faster than ~the poll tick.
        assert per_call_s >= 0.05
