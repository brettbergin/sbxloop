"""`.github/workflows/deploy.yml` pins (#534, #530).

The pipeline runs unattended on a root-equivalent host and cannot be
exercised here, so its load-bearing lines are pinned as text: it must never
restart the daemon under a live run, its hold must never outlive the job,
and no job on the self-hosted runner may be reachable from a fork.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy.yml"


@pytest.fixture(scope="module")
def deploy() -> str:
    return WORKFLOW.read_text()


def _step(deploy: str, name: str) -> str:
    """The text of one step, from its `- name:` to the next step's."""
    pattern = rf"      - name: {re.escape(name)}\n(.*?)(?=\n      - name: |\Z)"
    match = re.search(pattern, deploy, re.S)
    assert match, f"step {name!r} missing"
    return match.group(1)


class TestNeverRestartUnderARun:
    def test_the_drain_has_no_cap_and_no_restart_anyway_branch(self, deploy: str) -> None:
        assert "restarting anyway" not in deploy
        assert "seq 1 80" not in deploy
        wait = _step(deploy, "Wait for the daemon to go idle")
        assert "while :; do" in wait and 'if [ "${current}" = "idle" ]' in wait
        # Only a daemon that answers nothing at all lets the restart proceed.
        assert "misses" in wait and 'echo "waited_s=${waited}"' in wait
        # #309: status() mutates the breaker; never poll faster than 15s.
        assert "sleep 15" in wait

    def test_the_job_timeout_covers_a_long_run(self, deploy: str) -> None:
        match = re.search(r"timeout-minutes: (\d+)", deploy)
        assert match and int(match.group(1)) >= 240

    def test_the_wait_precedes_the_upgrade_and_the_restart(self, deploy: str) -> None:
        order = [
            "Take the deploy hold",
            "Wait for the daemon to go idle",
            "Upgrade",
            "Snapshot the holds to restore",
            "Restart the daemon",
            "Health check",
            "Roll back",
            "Restore the other holds",
            "Release the deploy hold",
        ]
        positions = [deploy.index(f"      - name: {name}\n") for name in order]
        assert positions == sorted(positions)

    def test_rollback_only_after_the_upgrade_ran(self, deploy: str) -> None:
        rollback = _step(deploy, "Roll back")
        assert "steps.upgrade.outcome != 'skipped'" in rollback
        assert "id: upgrade" in _step(deploy, "Upgrade")


class TestNamedHolds:
    def test_the_deploy_takes_and_releases_its_own_named_hold(self, deploy: str) -> None:
        assert "HOLD: deploy-${{ github.run_id }}" in deploy
        assert 'ctl pause --hold "${HOLD}"' in _step(deploy, "Take the deploy hold")
        release = _step(deploy, "Release the deploy hold")
        assert "if: always() && steps.hold.outcome == 'success'" in release
        assert 'ctl resume --hold "${HOLD}"' in release
        # Never a bare pause/resume: that is the operator's hold.
        assert not re.search(r"ctl pause( --timeout|\s*\|\|)", deploy)
        assert not re.search(r"ctl resume( --timeout|\s*\|\|)", deploy)

    def test_other_holds_are_snapshotted_right_before_the_restart(self, deploy: str) -> None:
        snapshot = _step(deploy, "Snapshot the holds to restore")
        assert "grep -i '^holds:'" in snapshot and '[ "${h}" = "${HOLD}" ] && continue' in snapshot
        restore = _step(deploy, "Restore the other holds")
        assert "if: always()" in restore and 'ctl pause --hold "${h}"' in restore


class TestSecurityInvariant:
    def test_no_fork_triggerable_event(self, deploy: str) -> None:
        on = deploy[deploy.index("\non:\n") : deploy.index("\nconcurrency:")]
        assert "workflow_run:" in on and "workflow_dispatch:" in on
        for event in ("pull_request", "pull_request_target", "issue_comment", "push"):
            assert f"\n  {event}:" not in on
        assert "runs-on: [self-hosted, db]" in deploy
        assert "uses: actions/checkout" not in deploy
