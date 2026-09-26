"""CI verdicts must reject missing evidence, including after a failed shard."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def checks():
    spec = importlib.util.spec_from_file_location("ci_checks", ROOT / "scripts/ci_checks.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", None])
def test_dependency_verdict_rejects_every_non_success(checks, result):
    with pytest.raises(ValueError, match="pytest"):
        checks.dependencies({"pytest": {"result": result}}, ["pytest"])


def test_dependency_verdict_rejects_missing_job(checks):
    with pytest.raises(ValueError, match="pytest"):
        checks.dependencies({}, ["pytest"])
    checks.dependencies({"pytest": {"result": "success"}}, ["pytest"])


def test_coverage_requires_every_slice_and_rejects_empty_files(checks, tmp_path):
    slices = ["fast-1", "fast-2", "slow-1"]
    for name in slices[:-1]:
        (tmp_path / f".coverage.{name}").write_bytes(b"data")
    with pytest.raises(ValueError, match="slow-1"):
        checks.coverage_files(tmp_path, slices)
    (tmp_path / ".coverage.slow-1").touch()
    with pytest.raises(ValueError, match="slow-1"):
        checks.coverage_files(tmp_path, slices)
    (tmp_path / ".coverage.slow-1").write_bytes(b"data")
    checks.coverage_files(tmp_path, slices)
    (tmp_path / ".coverage.stale").write_bytes(b"data")
    with pytest.raises(ValueError, match="stale"):
        checks.coverage_files(tmp_path, slices)


def test_required_checks_run_after_failed_dependencies_and_keep_names():
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    assert jobs["test"]["if"] == "${{ always() }}"
    assert jobs["test"]["strategy"]["matrix"]["python-version"] == ["3.13", "3.14"]
    assert set(jobs["test"]["needs"]) == {"pytest", "codex-sdk", "openai-sdk", "playwright-preset"}
    assert jobs["verified"]["if"] == "${{ always() }}"
    assert set(jobs["verified"]["needs"]) == {
        "lint",
        "typecheck",
        "test",
        "build",
        "windows-host",
    }
    assert all(name in jobs for name in ("lint", "typecheck", "build", "windows-host"))


def test_five_mixed_shards_cover_the_whole_collection_and_supply_the_verdict():
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    matrix = jobs["pytest"]["strategy"]["matrix"]
    assert matrix["shard"] == [1, 2, 3, 4, 5]
    command = next(
        step["run"] for step in jobs["pytest"]["steps"] if " pytest " in step.get("run", "")
    )
    assert "--shard ${{ matrix.shard }}/5" in command
    assert " -m " not in command
    coverage = next(
        step["run"]
        for step in jobs["test"]["steps"]
        if "ci_checks.py coverage" in step.get("run", "")
    )
    assert coverage.split("coverage ", 1)[1].split() == [str(n) for n in matrix["shard"]]


def test_reusable_ci_checks_out_the_requested_commit_with_locked_dependencies():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert "workflow_call" in workflow[True]
    assert "github.workflow" in workflow["concurrency"]["group"]
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("actions/checkout@"):
                expected = (
                    "${{ github.workflow_sha }}"
                    if step["with"].get("path") == ".ci-tools"
                    else "${{ inputs.sha || github.sha }}"
                )
                assert step["with"]["ref"] == expected
            run = step.get("run", "")
            if "uv sync " in run:
                assert "--locked" in run
            if "uv run " in run:
                assert "--no-sync" in run


def test_release_fallback_uses_ci_and_publication_requires_a_successful_verdict():
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    assert jobs["verify"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["verify"]["with"]["sha"] == "${{ needs.batch.outputs.sha }}"
    assert set(jobs["check"]["needs"]) == {"evidence", "verify"}
    assert "always()" in jobs["check"]["if"]
    assert "check" in jobs["release"]["needs"]
    # A deliberately skipped fallback must not propagate a skip through
    # the successful check into publication; override implicit success().
    assert "!cancelled()" in jobs["release"]["if"]
    assert "needs.check.result == 'success'" in jobs["release"]["if"]


@pytest.mark.parametrize(
    "evidence,reused,verify,ok",
    [
        ("success", "true", "skipped", True),
        ("success", "false", "success", True),
        ("failure", "true", "skipped", False),
        ("cancelled", "true", "skipped", False),
        ("success", "false", "skipped", False),
        ("success", "", "success", False),
        ("success", "false", "failure", False),
        ("success", "false", "cancelled", False),
    ],
)
def test_publication_verdict_executes_fail_closed(evidence, reused, verify, ok):
    bash = shutil.which("bash")
    if bash is None or os.name == "nt":
        pytest.skip("Actions runs this on Linux")
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    step = jobs["check"]["steps"][0]
    result = subprocess.run(
        [bash, "-e", "-c", step["run"]],
        check=False,
        capture_output=True,
        env={**os.environ, "EVIDENCE_RESULT": evidence, "REUSED": reused, "VERIFY_RESULT": verify},
    )
    assert (result.returncode == 0) is ok


def test_noop_release_skips_verification_without_failing_the_workflow():
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    assert "needs.evidence.result != 'skipped'" in jobs["check"]["if"]


def test_automatic_intake_is_separate_from_the_manual_publication_lock():
    wake = yaml.safe_load((ROOT / ".github/workflows/release-wakeup.yml").read_text())
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    assert set(wake[True]) == {"push", "schedule"}
    assert wake["concurrency"]["group"] != release["concurrency"]["group"]
    assert wake["concurrency"]["cancel-in-progress"] is False
    assert wake["concurrency"].get("queue", "single") == "single"
    assert set(release[True]) == {"workflow_dispatch"}
    assert release["concurrency"]["queue"] == "max"
    assert release["jobs"]["release"]["environment"] == "pypi"


def test_final_release_wheels_are_smoked_before_staging_or_publication():
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    steps = jobs["release"]["steps"]
    smoke = next(i for i, step in enumerate(steps) if "smoke_wheels.py" in step.get("run", ""))
    stage = next(
        i for i, step in enumerate(steps) if step.get("name", "").startswith("Stage original")
    )
    assert smoke < stage
    assert "if" not in steps[smoke]  # Original retry bytes are smoked too.


def test_verdict_helpers_come_from_the_workflow_revision_for_old_release_reservations():
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    for name in ("test", "verified"):
        steps = jobs[name]["steps"]
        helper = next(step for step in steps if step.get("with", {}).get("path") == ".ci-tools")
        assert helper["with"]["ref"] == "${{ github.workflow_sha }}"
        assert helper["with"]["sparse-checkout"] == "scripts/ci_checks.py"
        for step in steps:
            if "ci_checks.py" in step.get("run", ""):
                assert ".ci-tools/scripts/ci_checks.py" in step["run"]
