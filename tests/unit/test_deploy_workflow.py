"""`.github/workflows/deploy.yml` pins (#534, #530, #619, #639, #640) and
the generic copy in `contrib/workflows/deploy-daemon.yml.example`.

The pipeline runs unattended on a root-equivalent host and cannot be
exercised here, so its load-bearing lines are pinned as text: it must never
restart the daemon under a live run, its hold must never outlive the job,
no job on the self-hosted runner may be reachable from a fork, its rollback
must install the same extras as its upgrade, it must read the daemon
through `ctl status --json` / `daemon notify` rather than prose, secrets
and config files, and nothing in it may name a host or a user.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
EXAMPLE = ROOT / "contrib" / "workflows" / "deploy-daemon.yml.example"

_EXTRAS_RE = re.compile(r"sbxloop(?:-\S*?\.whl)?\[([\w,]+)\]")


@pytest.fixture(scope="module")
def deploy() -> str:
    return WORKFLOW.read_text()


@pytest.fixture(scope="module")
def example() -> str:
    return EXAMPLE.read_text()


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

    def test_an_answer_without_json_fails_before_anything_is_installed(self, deploy: str) -> None:
        # Exit 1 = the daemon answered, but with prose (pre-#639) or late.
        # Guessing "idle" there is the restart-under-a-run this file exists
        # to prevent; it fails the job instead, with nothing installed yet.
        wait = _step(deploy, "Wait for the daemon to go idle")
        assert 'elif [ "${rc}" -eq 1 ]; then' in wait
        assert "exit 1" in wait.split('elif [ "${rc}" -eq 1 ]; then')[1].split("else")[0]

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
        assert "jq -r '.holds // [] | .[]'" in snapshot
        assert '[ "${h}" = "${HOLD}" ] && continue' in snapshot
        restore = _step(deploy, "Restore the other holds")
        assert "if: always()" in restore and 'ctl pause --hold "${h}"' in restore


class TestSecurityInvariant:
    def test_no_fork_triggerable_event(self, deploy: str) -> None:
        on = deploy[deploy.index("\non:\n") : deploy.index("\nconcurrency:")]
        assert "workflow_run:" in on and "workflow_dispatch:" in on
        for event in ("pull_request", "pull_request_target", "issue_comment", "push"):
            assert f"\n  {event}:" not in on
        assert "runs-on: [self-hosted, " in deploy
        assert "uses: actions/checkout" not in deploy

    def test_the_example_has_no_fork_triggerable_event(self, example: str) -> None:
        on = example[example.index("\non:\n") : example.index("\nconcurrency:")]
        assert "schedule:" in on and "workflow_dispatch:" in on
        for event in ("pull_request", "pull_request_target", "issue_comment", "push"):
            assert f"\n  {event}:" not in on
        assert "uses: actions/checkout" not in example


class TestRollbackExtrasParity:
    """#619: a rollback that drops a chat extra rolls a host on that backend
    back to a build that cannot start."""

    @pytest.mark.parametrize("fixture", ["deploy", "example"])
    def test_rollback_installs_the_same_extras_as_the_upgrade(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        upgrade = _EXTRAS_RE.findall(_step(text, "Upgrade"))
        rollback = _EXTRAS_RE.findall(_step(text, "Roll back"))
        assert upgrade and rollback
        assert set(upgrade[0].split(",")) == set(rollback[0].split(",")) == {"discord", "slack"}


class TestStructuredControl:
    """#639: the job reads the daemon through `ctl status --json` and
    speaks through `daemon notify` — never prose, the secrets file or the
    daemon's config."""

    @pytest.mark.parametrize("fixture", ["deploy", "example"])
    def test_no_step_reads_secrets_or_config(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        for forbidden in (
            "secrets.env",
            "sbxloop.toml",
            "DISCORD_BOT_TOKEN",
            "SLACK_BOT_TOKEN",
            "discord.com/api",
            "slack.com/api",
            "grep -i '^current:'",
            "grep -i '^holds:'",
            "^paused:",
        ):
            assert forbidden not in text, forbidden

    @pytest.mark.parametrize("fixture", ["deploy", "example"])
    def test_status_is_read_as_json_and_notices_go_through_notify(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        for name in ("Take the deploy hold", "Wait for the daemon to go idle"):
            assert "daemon ctl status --json" in _step(text, name), name
        assert "daemon ctl status --json" in _step(text, "Snapshot the holds to restore")
        assert 'jq -r "${CURRENT_JQ}"' in _step(text, "Wait for the daemon to go idle")
        for name in ("Announce", "Report"):
            step = _step(text, name)
            assert 'daemon notify "${MSG}"' in step, name
            # A chat outage must not fail (or roll back) a deploy.
            assert "continue-on-error: true" in step, name


class TestHostAgnostic:
    """#640: the host is one repository variable; nothing names a machine,
    a user or a home directory."""

    def test_the_host_is_one_variable(self, deploy: str) -> None:
        assert "runs-on: [self-hosted, \"${{ vars.SBXLOOP_DEPLOY_HOST || 'db' }}\"]" in deploy
        assert "HOST: ${{ vars.SBXLOOP_DEPLOY_HOST || 'db' }}" in deploy
        # The default is the only place the dogfood host's name appears.
        assert len(re.findall(r"\bdb\b", deploy)) == 3  # the two above + the comment

    @pytest.mark.parametrize("fixture", ["deploy", "example"])
    def test_no_usernames_home_directories_or_hostnames(
        self, fixture: str, request: pytest.FixtureRequest
    ) -> None:
        text = request.getfixturevalue(fixture)
        assert "/home/" not in text
        assert "bergs" not in text
        assert "ssh " not in text
        assert "${HOME}/.sbxloop-venv" in _step(text, "Resolve host paths")
        assert "needs a human" in _step(text, "Roll back")
        assert "${HOST} needs a human" in _step(text, "Roll back")

    def test_the_example_names_nothing_at_all(self, example: str) -> None:
        assert "brettbergin" not in example
        assert not re.search(r"\bdb\b", example)
        assert "|| 'db'" not in example
        assert 'runs-on: [self-hosted, "${{ vars.SBXLOOP_DEPLOY_HOST }}"]' in example
        assert "pypi.org/pypi/sbxloop/json" in _step(example, "Resolve the target version")


class TestDocsSplit:
    """#642: the generic guide names no host, user or repository; the
    systemd README's upgrade section leads with the manual path."""

    @pytest.fixture
    def guide(self) -> str:
        return (ROOT / "docs" / "deploy.md").read_text()

    @pytest.fixture
    def systemd_readme(self) -> str:
        return (ROOT / "contrib" / "systemd" / "README.md").read_text()

    def test_generic_guide_names_nothing(self, guide: str) -> None:
        assert "brettbergin" not in guide
        assert not re.search(r"\bdb\b", guide)
        assert "ssh " not in guide and "/home/" not in guide
        assert "deploy.yml" not in guide  # that is the self-deploy reference's
        assert "<owner>/<repo>" in guide and "SBXLOOP_DEPLOY_HOST" in guide

    def test_generic_guide_points_at_the_self_deploy_reference(self, guide: str) -> None:
        assert "self-deploy.md" in guide
        reference = (ROOT / "docs" / "self-deploy.md").read_text()
        assert "brettbergin/sbxloop" in reference and "SBXLOOP_DEPLOY_HOST" in reference
        assert "#639" in reference  # the structured-status cutover note

    def test_systemd_upgrade_section_leads_with_the_manual_path(self, systemd_readme: str) -> None:
        section = systemd_readme.split("## Upgrading", 1)[1].split("\n## ", 1)[0]
        first_fence = section.split("```bash", 1)[1].split("```", 1)[0]
        assert "pip install --upgrade 'sbxloop[discord,slack]==X.Y.Z'" in first_fence
        assert "ctl status --json" in first_fence
        assert "reset-failed sbxloop-daemon" in first_fence
        # The workflow is the optional afterthought, below the commands.
        assert section.index("deploy-daemon.yml.example") > section.index("reset-failed")
        assert "Automated, via" not in section

    def test_runner_unit_is_marked_self_deploy_only(self) -> None:
        unit = (ROOT / "contrib" / "systemd" / "github-runner.service").read_text()
        assert unit.startswith("# Only needed for the automated upgrade workflow")

    def test_cutover_note_lives_in_the_changelog(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text()
        assert "### 1.0 cutover" in changelog
        readme = (ROOT / "README.md").read_text()
        assert "CHANGELOG.md#10-cutover" in readme
