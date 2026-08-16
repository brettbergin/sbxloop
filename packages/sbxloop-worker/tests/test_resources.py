"""Resource telemetry tests: heartbeat sampling, guardrail levels, and the
disk-abort result rewrite (issue #54)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

import sbxloop_worker.runner as runner_mod
from sbxloop_worker.protocol import Event, EventTypes, JobRequest
from sbxloop_worker.resources import classify_level, sample_resources
from sbxloop_worker.runner import JobRunner


def agent_job(**overrides: object) -> JobRequest:
    base: dict[str, object] = {
        "job_id": "j1",
        "run_id": "r1",
        "kind": "agent.session",
        "prompt": "hello",
    }
    base.update(overrides)
    return JobRequest.model_validate(base)


def failing_job() -> JobRequest:
    # github.op with no params fails fast with GithubOpError — a stand-in
    # for "whatever confusing way tooling fails on a full disk".
    return JobRequest(job_id="j1", run_id="r1", kind="github.op", op="issue.create")


def run_with_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample: dict[str, Any],
    *,
    job: JobRequest | None = None,
    **runner_kwargs: float,
) -> tuple[Any, list[Event]]:
    """Run a job with a deterministic resource sample. heartbeat_s is large
    so only the synchronous baseline sample fires."""
    monkeypatch.setattr(runner_mod, "sample_resources", lambda path=".": dict(sample))
    monkeypatch.setenv("SBXLOOP_WORKER_BACKEND", "echo")
    events_path = tmp_path / "events.jsonl"
    result = JobRunner(
        job if job is not None else agent_job(),
        events_path=events_path,
        result_path=tmp_path / "result.json",
        heartbeat_s=3600.0,
        backend_name="echo",
        **runner_kwargs,
    ).run()
    events = [Event.from_json_line(line) for line in events_path.read_text().splitlines()]
    return result, events


class TestSampleResources:
    def test_disk_fields_present(self) -> None:
        sample = sample_resources(".")
        assert 0.0 <= sample["disk_used_pct"] <= 100.0
        assert sample["disk_total_bytes"] > 0
        assert sample["disk_free_bytes"] >= 0

    def test_unreadable_path_omits_disk(self) -> None:
        sample = sample_resources("/nonexistent/definitely/nope")
        assert "disk_used_pct" not in sample


class TestClassifyLevel:
    LIMITS: ClassVar[dict[str, float]] = {"disk_warn": 85.0, "disk_abort": 95.0, "mem_warn": 90.0}

    def test_zero_thresholds_disable(self) -> None:
        assert classify_level({"disk_used_pct": 99.9, "mem_used_pct": 99.9}) == "ok"

    def test_disk_levels(self) -> None:
        assert classify_level({"disk_used_pct": 84.9}, **self.LIMITS) == "ok"
        assert classify_level({"disk_used_pct": 85.0}, **self.LIMITS) == "warn"
        assert classify_level({"disk_used_pct": 95.0}, **self.LIMITS) == "abort"

    def test_memory_only_warns_without_mem_abort(self) -> None:
        assert classify_level({"mem_used_pct": 99.0}, **self.LIMITS) == "warn"

    def test_mem_abort_levels(self) -> None:
        # #253: memory abort is opt-in — the host passes 0 unless configured.
        limits = {**self.LIMITS, "mem_abort": 97.0}
        assert classify_level({"mem_used_pct": 96.9}, **limits) == "warn"
        assert classify_level({"mem_used_pct": 97.0}, **limits) == "abort"
        assert classify_level({"disk_used_pct": 10.0, "mem_used_pct": 99.0}, **limits) == "abort"

    def test_empty_sample_is_ok(self) -> None:
        assert classify_level({}, **self.LIMITS) == "ok"


class TestRunnerTelemetry:
    def test_baseline_sample_emitted_after_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, events = run_with_sample(tmp_path, monkeypatch, {"disk_used_pct": 10.0})
        assert result.status == "ok"
        types = [e.type for e in events]
        assert types[0] == EventTypes.WORKER_START
        assert EventTypes.SANDBOX_RESOURCES in types
        sample_event = next(e for e in events if e.type == EventTypes.SANDBOX_RESOURCES)
        assert sample_event.data["level"] == "ok"
        assert sample_event.data["disk_used_pct"] == 10.0
        assert EventTypes.SANDBOX_RESOURCES_WARNING not in types

    def test_no_sampling_when_heartbeat_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SBXLOOP_WORKER_BACKEND", "echo")
        events_path = tmp_path / "events.jsonl"
        JobRunner(
            agent_job(),
            events_path=events_path,
            result_path=tmp_path / "result.json",
            heartbeat_s=0.0,
            backend_name="echo",
            disk_warn=85.0,
            disk_abort=95.0,
        ).run()
        events = [Event.from_json_line(line) for line in events_path.read_text().splitlines()]
        assert EventTypes.SANDBOX_RESOURCES not in [e.type for e in events]

    def test_warn_threshold_emits_edge_triggered_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, events = run_with_sample(
            tmp_path,
            monkeypatch,
            {"disk_used_pct": 90.0},
            disk_warn=85.0,
            disk_abort=95.0,
        )
        assert result.status == "ok"  # warn never fails a job
        warnings = [e for e in events if e.type == EventTypes.SANDBOX_RESOURCES_WARNING]
        assert len(warnings) == 1
        assert warnings[0].data["level"] == "warn"
        assert "disk 90.0% used" in warnings[0].data["message"]

    def test_abort_rewrites_failed_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, events = run_with_sample(
            tmp_path,
            monkeypatch,
            {"disk_used_pct": 97.0},
            job=failing_job(),
            disk_warn=85.0,
            disk_abort=95.0,
        )
        assert result.status == "error"
        assert result.error.type == "SandboxResourcesExhausted"
        assert "sandbox disk exhausted" in result.error.message
        assert "97.0%" in result.error.message
        # The original failure is preserved for diagnosis, not discarded.
        assert "GithubOpError" in (result.error.detail or "")
        warnings = [e for e in events if e.type == EventTypes.SANDBOX_RESOURCES_WARNING]
        assert warnings and warnings[0].data["level"] == "abort"

    def test_abort_never_rewrites_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, _ = run_with_sample(
            tmp_path,
            monkeypatch,
            {"disk_used_pct": 97.0},
            disk_warn=85.0,
            disk_abort=95.0,
        )
        assert result.status == "ok"

    def test_mem_pressure_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        result, events = run_with_sample(
            tmp_path,
            monkeypatch,
            {"disk_used_pct": 10.0, "mem_used_pct": 95.0},
            mem_warn=90.0,
        )
        assert result.status == "ok"
        warnings = [e for e in events if e.type == EventTypes.SANDBOX_RESOURCES_WARNING]
        assert len(warnings) == 1
        assert "memory 95.0% used" in warnings[0].data["message"]

    def test_mem_abort_rewrites_failed_result_naming_memory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #253: an OOM-bound job is diagnosed as memory, not as a full disk.
        result, events = run_with_sample(
            tmp_path,
            monkeypatch,
            {"disk_used_pct": 10.0, "mem_used_pct": 98.5},
            job=failing_job(),
            disk_warn=85.0,
            disk_abort=95.0,
            mem_warn=90.0,
            mem_abort=97.0,
        )
        assert result.status == "error"
        assert result.error.type == "SandboxResourcesExhausted"
        assert "sandbox memory exhausted" in result.error.message
        assert "98.5%" in result.error.message
        assert "disk" not in result.error.message
        warnings = [e for e in events if e.type == EventTypes.SANDBOX_RESOURCES_WARNING]
        assert warnings and warnings[0].data["level"] == "abort"

    def test_disk_named_when_both_abort_thresholds_trip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, _ = run_with_sample(
            tmp_path,
            monkeypatch,
            {"disk_used_pct": 99.0, "mem_used_pct": 99.0},
            job=failing_job(),
            disk_abort=95.0,
            mem_abort=97.0,
        )
        assert result.status == "error"
        assert "sandbox disk exhausted" in result.error.message
