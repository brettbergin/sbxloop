"""Real SDK serialization/notification routing against a fake stdio peer.

The optional pinned SDK is exercised, but its bundled Codex executable is
never launched. No model, network request or sandbox participates.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sbxloop_worker.backends.codex import CodexBackend
from sbxloop_worker.protocol import EventTypes, JobRequest

pytestmark = pytest.mark.slow
FAKE_SERVER = Path(__file__).resolve().parents[1] / "fakes" / "fake_codex_stdio.py"
TEST_KEY = "sk-test-stdio-contract-key"


@dataclass
class SdkHarness:
    transcript: Path
    scenario: str = "complete"
    configs: list[Any] = field(default_factory=list)
    processes: list[subprocess.Popen[str]] = field(default_factory=list)

    def messages(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.transcript.read_text().splitlines()]


@pytest.fixture
def sdk_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SdkHarness:
    client_module = pytest.importorskip("openai_codex.client")
    pytest.importorskip("codex_cli_bin")
    harness = SdkHarness(tmp_path / "requests.jsonl")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "operator-codex-home"))
    original_config = client_module.CodexConfig
    original_popen = subprocess.Popen

    def configure(**kwargs: Any) -> Any:
        config = original_config(**kwargs)
        config.launch_args_override = (
            sys.executable,
            str(FAKE_SERVER),
            str(harness.transcript),
            harness.scenario,
        )
        harness.configs.append(config)
        return config

    def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        assert list(args[0]) == list(harness.configs[-1].launch_args_override)
        process = original_popen(*args, **kwargs)
        harness.processes.append(process)
        return process

    def refuse_real_binary(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("the contract test must never resolve or run the real Codex executable")

    monkeypatch.setattr(client_module, "CodexConfig", configure)
    monkeypatch.setattr(client_module, "_resolve_codex_bin", refuse_real_binary)
    monkeypatch.setattr(client_module.subprocess, "Popen", spawn)
    return harness


def make_job(tmp_path: Path, **overrides: Any) -> JobRequest:
    (tmp_path / "input.txt").write_text("fixture evidence\n")
    return JobRequest(
        job_id="sdk-contract",
        run_id="run-sdk-contract",
        kind="agent.session",
        prompt="Read the fixture and report JSON.",
        cwd=str(tmp_path),
        permission_mode="read_only",
        expect="json",
        **{"timeout_s": 10.0, **overrides},
    )


def test_real_sdk_preserves_dynamic_tools_and_typed_turn_events(
    tmp_path: Path, sdk_harness: SdkHarness
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    job = make_job(tmp_path, system_message="Workload instructions", system_preset=False)
    result = CodexBackend().run_session(job, lambda event, **data: events.append((event, data)))
    assert result.output_json == {"verified": True}
    assert result.session_id
    assert result.usage is not None
    assert result.usage.model == "fixture-model"
    assert result.usage.input_tokens == 15
    assert result.usage.output_tokens == 6
    assert result.usage.cache_read_tokens == 4
    assert [event for event, _ in events].count(EventTypes.AGENT_MESSAGE) == 1
    assert any(event == EventTypes.AGENT_MESSAGE_DELTA for event, _ in events)
    assert any(event == EventTypes.AGENT_TOOL_END and data["success"] for event, data in events)
    messages = sdk_harness.messages()
    requests = {
        message.get("method"): message.get("params") for message in messages if "method" in message
    }
    assert requests["initialize"]["capabilities"]["experimentalApi"] is True
    assert requests["config/read"] == {"cwd": str(tmp_path), "includeLayers": False}
    assert requests["account/login/start"] == {"type": "apiKey", "apiKey": TEST_KEY}
    thread = requests["thread/start"]
    assert thread["environments"] == []
    assert thread["baseInstructions"] == "Workload instructions"
    assert {tool["name"] for tool in thread["dynamicTools"]} == {
        "read_file",
        "list_files",
        "search_files",
    }
    turn = requests["turn/start"]
    assert turn["threadId"] == "thread-fixture"
    assert turn["environments"] == []
    assert turn["input"] == [{"type": "text", "text": job.prompt}]
    tool_response = next(
        message["result"] for message in messages if message.get("id") == "fixture-tool-request"
    )
    assert tool_response["success"] is True
    assert "fixture evidence" in tool_response["contentItems"][0]["text"]
    assert all(process.poll() is not None for process in sdk_harness.processes)
    config = sdk_harness.configs[0]
    assert config.env["CODEX_HOME"] != str(tmp_path / "operator-codex-home")
    assert TEST_KEY not in json.dumps(list(config.launch_args_override))
    assert TEST_KEY not in json.dumps(config.config_overrides)


def test_real_sdk_resume_uses_the_requested_session(
    tmp_path: Path, sdk_harness: SdkHarness
) -> None:
    previous = CodexBackend().run_session(make_job(tmp_path), lambda *args, **kwargs: None)
    assert previous.session_id
    sdk_harness.transcript.write_text("")
    result = CodexBackend().run_session(
        make_job(tmp_path, resume_session_id=previous.session_id), lambda *args, **kwargs: None
    )
    assert result.session_id == previous.session_id
    requests = [message for message in sdk_harness.messages() if "method" in message]
    assert not any(message["method"] == "thread/start" for message in requests)
    resume = next(message["params"] for message in requests if message["method"] == "thread/resume")
    assert resume["threadId"] == "thread-fixture"
    assert all(process.poll() is not None for process in sdk_harness.processes)


@pytest.mark.parametrize("scenario", ["hang-login", "hang-turn"])
def test_real_sdk_waits_are_bounded_and_the_process_closes(
    tmp_path: Path, sdk_harness: SdkHarness, scenario: str
) -> None:
    sdk_harness.scenario = scenario
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        CodexBackend().run_session(make_job(tmp_path, timeout_s=0.5), lambda *args, **kwargs: None)
    assert time.monotonic() - started < 5
    assert sdk_harness.processes
    assert all(process.poll() is not None for process in sdk_harness.processes)
    requests = [message["method"] for message in sdk_harness.messages() if "method" in message]
    assert "account/login/start" in requests
    assert ("turn/start" in requests) == (scenario == "hang-turn")
