"""Provisioner tests: specs, token split, policy, secrets, rollback."""

from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.errors import ProvisionError
from sbxloop.events import Event, EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner, sandbox_name
from sbxloop.sbx.sandbox import WORK_DIR
from tests.conftest import FakeSbx

TOKENS = {"COPILOT_GITHUB_TOKEN": "github_pat_copilot", "GH_TOKEN": "github_pat_user"}

# Most tests exercise the full two-sandbox pair, which now requires the
# GitHub integration to be configured.
GITHUB_ENABLED = {"github": {"repo": "owner/repo"}}


def make_provisioner(
    fake_sbx: FakeSbx,
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    config: Config | None = None,
    bus: EventBus | None = None,
) -> Provisioner:
    config = config or Config.model_validate(
        {"state_dir": str(tmp_path / "state"), **GITHUB_ENABLED}
    )
    return Provisioner(
        SbxCLI(binary=str(fake_sbx.binary)),
        config,
        bus=bus,
        env=TOKENS if env is None else env,
    )


class TestSpecs:
    def test_build_specs_roles_and_domains(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        agent, github = provisioner.build_specs("r1", tmp_path)
        assert agent.name == "sbxloop-r1-agent"
        assert github.name == "sbxloop-r1-github"
        assert "api.githubcopilot.com" in agent.policy_allows
        assert "uploads.github.com" in github.policy_allows
        # The prompt-advertised baseline (PyPI + apt mirrors) is granted to
        # the agent sandbox up front: worker pip installs and the dev-tools
        # apt ensure run before any plan-declared egress exists, so they
        # must not depend on the operator's global sbx preset.
        for host in ("pypi.org", "archive.ubuntu.com", "security.ubuntu.com"):
            assert host in agent.policy_allows
        assert "archive.ubuntu.com" not in github.policy_allows
        assert {s.kind for s in agent.secrets} == {"custom"}
        assert [s.service for s in github.secrets] == ["github"]
        # ONE custom secret, bound to the token-exchange host only: sbx keys
        # custom secrets by env name, so the same env cannot bind two hosts;
        # the exchanged Copilot token lives in SDK memory, so the copilot API
        # hosts need network allows but no env rewrite
        assert [(s.host, s.env) for s in agent.secrets] == [
            ("api.github.com", "COPILOT_GITHUB_TOKEN")
        ]

    def test_denied_baseline_domain_is_never_seeded(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # The always-reachable tier is seeded before any plan exists, so
        # refusing a grant later cannot enforce [policy] deny against it —
        # the domain has to be kept out of the spec (#141).
        config = Config.model_validate({"policy": {"deny": ["pypi.org", "*.ubuntu.com"]}})
        provisioner = make_provisioner(fake_sbx, tmp_path, config=config)
        agent, _github = provisioner.build_specs("r1", tmp_path)
        assert "pypi.org" not in agent.policy_allows
        assert "archive.ubuntu.com" not in agent.policy_allows
        # ...while the rest of the baseline, and sbxloop's own control
        # plane, are untouched.
        assert "files.pythonhosted.org" in agent.policy_allows
        assert "deb.debian.org" in agent.policy_allows
        assert "api.githubcopilot.com" in agent.policy_allows

    def test_extra_allow_domains_added(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        config = Config.model_validate(
            {"sandbox": {"extra_allow_domains": ["internal.example.com"]}}
        )
        provisioner = make_provisioner(fake_sbx, tmp_path, config=config)
        agent, github = provisioner.build_specs("r1", tmp_path)
        assert "internal.example.com" in agent.policy_allows
        assert "internal.example.com" in github.policy_allows


class TestTokens:
    def test_missing_copilot_token(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path, env={"GH_TOKEN": "x"})
        with pytest.raises(ProvisionError, match="COPILOT_GITHUB_TOKEN is not set"):
            provisioner.ensure_pair("r1")

    def test_missing_gh_token(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path, env={"COPILOT_GITHUB_TOKEN": "x"})
        with pytest.raises(ProvisionError, match="GH_TOKEN, GITHUB_TOKEN"):
            provisioner.ensure_pair("r1")

    def test_github_token_fallback(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(
            fake_sbx, tmp_path, env={"COPILOT_GITHUB_TOKEN": "a", "GITHUB_TOKEN": "b"}
        )
        assert provisioner.gh_token() == "b"

    def test_no_sandbox_created_when_tokens_missing(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path, env={})
        with pytest.raises(ProvisionError):
            provisioner.ensure_pair("r1")
        assert fake_sbx.invocations("create") == []


class TestGithubGating:
    """Without [github].repo there is no github sandbox and no GH_TOKEN need."""

    def unconfigured(self, fake_sbx: FakeSbx, tmp_path: Path, **kwargs: object) -> Provisioner:
        config = Config.model_validate({"state_dir": str(tmp_path / "state")})
        assert not config.github.enabled
        return make_provisioner(fake_sbx, tmp_path, config=config, **kwargs)  # type: ignore[arg-type]

    def test_agent_only_when_unconfigured(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = self.unconfigured(
            fake_sbx, tmp_path, env={"COPILOT_GITHUB_TOKEN": "github_pat_x"}
        )
        pair = provisioner.ensure_pair("r1")
        try:
            assert pair.github is None
            assert pair.agent.name == sandbox_name("r1", "agent")
            created = [c[1].removeprefix("--name=") for c in fake_sbx.invocations("create")]
            assert created == [sandbox_name("r1", "agent")]
        finally:
            pair.cleanup()

    def test_no_gh_token_required_when_unconfigured(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # GH_TOKEN absent entirely: provisioning must not even ask for it.
        provisioner = self.unconfigured(
            fake_sbx, tmp_path, env={"COPILOT_GITHUB_TOKEN": "github_pat_x"}
        )
        pair = provisioner.ensure_pair("r1")
        pair.cleanup()

    def test_gh_token_still_required_when_configured(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        provisioner = make_provisioner(
            fake_sbx, tmp_path, env={"COPILOT_GITHUB_TOKEN": "github_pat_x"}
        )
        with pytest.raises(ProvisionError, match="GH_TOKEN"):
            provisioner.ensure_pair("r1")

    def test_cleanup_with_no_github_sandbox(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = self.unconfigured(
            fake_sbx, tmp_path, env={"COPILOT_GITHUB_TOKEN": "github_pat_x"}
        )
        pair = provisioner.ensure_pair("r1")
        pair.cleanup()
        assert fake_sbx.invocations("rm") != []


class TestEnsurePair:
    def test_full_provision_proxy_strategy(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)

        pair = provisioner.ensure_pair("r1")
        try:
            assert pair.agent.name == sandbox_name("r1", "agent")
            assert pair.github.name == sandbox_name("r1", "github")

            # both sandboxes exist and are running
            assert fake_sbx.meta(pair.agent.name)["status"] == "running"
            assert fake_sbx.meta(pair.github.name)["status"] == "running"

            # per-sandbox policy allows applied
            policies = fake_sbx.policies()
            assert [
                "allow",
                "network",
                "api.githubcopilot.com",
                "--sandbox",
                pair.agent.name,
            ] in policies
            assert [
                "allow",
                "network",
                "uploads.github.com",
                "--sandbox",
                pair.github.name,
            ] in policies

            # secrets: custom copilot token on agent (both hosts), github
            # service on github sandbox; values via the right channels
            secrets = fake_sbx.secrets()
            custom = [s for s in secrets if s["args"][0] == "set-custom"]
            service = [s for s in secrets if s["args"][0] == "set"]
            assert [s["args"][1] for s in custom] == [pair.agent.name]
            assert [s["args"][s["args"].index("--host") + 1] for s in custom] == ["api.github.com"]
            assert all("github_pat_copilot" in s["args"] for s in custom)
            assert service == [
                {
                    "args": ["set", pair.github.name, "github"],
                    "stdin": "github_pat_user",
                    "ts": service[0]["ts"],
                }
            ]

            # worker dirs created in both sandboxes
            for name in (pair.agent.name, pair.github.name):
                fs = fake_sbx.sandbox_fs(name)
                assert (fs / "home/agent/.sbxloop/jobs").is_dir()
                assert (fs / "home/agent/.sbxloop/results").is_dir()
                assert (fs / "home/agent/.sbxloop/events").is_dir()

            # workspace created under state dir
            assert (tmp_path / "state/runs/r1/workspace").is_dir()

            # events emitted
            types = [e.type for e in events]
            assert types.count("sandbox.provision_start") == 2
            assert types.count("sandbox.ready") == 2
        finally:
            pair.cleanup()

    def test_plain_env_strategy_writes_env_file(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        config = Config.model_validate(
            {"secret_strategy": "plain-env", "state_dir": str(tmp_path / "state"), **GITHUB_ENABLED}
        )
        provisioner = make_provisioner(fake_sbx, tmp_path, config=config)
        pair = provisioner.ensure_pair("r1")
        try:
            agent_env = (
                fake_sbx.sandbox_fs(pair.agent.name) / "home/agent/.sbxloop/env.sh"
            ).read_text()
            github_env = (
                fake_sbx.sandbox_fs(pair.github.name) / "home/agent/.sbxloop/env.sh"
            ).read_text()
            assert "export COPILOT_GITHUB_TOKEN=github_pat_copilot" in agent_env
            assert "GH_TOKEN" not in agent_env
            assert "export GH_TOKEN=github_pat_user" in github_env
            assert "export GITHUB_TOKEN=github_pat_user" in github_env
            assert "COPILOT_GITHUB_TOKEN" not in github_env
            # no sbx secret invocations under plain-env
            assert fake_sbx.secrets() == []
        finally:
            pair.cleanup()

    def test_keep_sandboxes_from_config(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        config = Config.model_validate(
            {"keep_sandboxes": True, "state_dir": str(tmp_path / "state")}
        )
        provisioner = make_provisioner(fake_sbx, tmp_path, config=config)
        pair = provisioner.ensure_pair("r1")
        assert pair.keep is True
        pair.cleanup()

    def test_post_create_hook_runs_per_sandbox(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        # The pair provisions on parallel threads, so hook order is not
        # deterministic — assert the set of calls, not their sequence.
        seen: list[tuple[str, str]] = []
        provisioner = make_provisioner(fake_sbx, tmp_path)
        provisioner.post_create = lambda sandbox, role: seen.append((sandbox.name, role))
        pair = provisioner.ensure_pair("r1")
        pair.cleanup()
        assert sorted(seen) == [
            ("sbxloop-r1-agent", "agent"),
            ("sbxloop-r1-github", "github"),
        ]

    def test_pair_provisions_concurrently(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        """Both sandboxes must be in flight at once (#127): each post_create
        waits at a two-party barrier, which only ever releases if the other
        sandbox's provisioning thread reaches it too."""
        import threading

        barrier = threading.Barrier(2)
        provisioner = make_provisioner(fake_sbx, tmp_path)
        provisioner.post_create = lambda sandbox, role: barrier.wait(timeout=30)
        pair = provisioner.ensure_pair("r1")
        pair.cleanup()
        assert not barrier.broken

    def test_rollback_on_secret_failure(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        # the github sandbox's secret application fails after both creates
        fake_sbx.fail_next("secret set sbxloop-r1-github", returncode=1, stderr="keychain locked")
        with pytest.raises(ProvisionError, match="provisioning run r1 failed"):
            provisioner.ensure_pair("r1")
        # everything created so far was rolled back
        assert not (fake_sbx.state / "sandboxes" / "sbxloop-r1-agent").exists()
        assert not (fake_sbx.state / "sandboxes" / "sbxloop-r1-github").exists()
        # ...including the agent's custom-secret registration, which would
        # otherwise be left owned by the now-deleted sandbox scope
        assert self.registered_custom_secrets(fake_sbx) == {}

    @staticmethod
    def registered_custom_secrets(fake_sbx: FakeSbx) -> dict[str, dict[str, str]]:
        import json

        state_path = fake_sbx.state / "secrets-state.json"
        if not state_path.is_file():
            return {}
        return dict(json.loads(state_path.read_text())["custom"])

    def test_rollback_unregisters_secrets_from_this_attempt(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """A failure after secrets were registered removes those
        registrations, symmetric with sandbox removal — the next run starts
        clean instead of relying on collision-recovery heuristics."""
        import json

        provisioner = make_provisioner(fake_sbx, tmp_path)
        # both sandboxes' secrets land; the github probe then dies at the
        # sbx level twice, which fails provisioning loudly
        fake_sbx.script(
            "exec sbxloop-r1-github sh -lc test -n",
            returncode=1,
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        )
        with pytest.raises(ProvisionError, match="probe failed twice"):
            provisioner.ensure_pair("r1")
        assert self.registered_custom_secrets(fake_sbx) == {}
        state = json.loads((fake_sbx.state / "secrets-state.json").read_text())
        assert state["service"] == {}
        # a fresh attempt provisions without needing any collision recovery
        rm_calls_before = len(fake_sbx.invocations("secret rm"))
        pair = provisioner.ensure_pair("r2")
        assert len(fake_sbx.invocations("secret rm")) == rm_calls_before
        pair.cleanup()

    def test_explicit_workspace_wins(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        workspace = tmp_path / "custom-ws"
        pair = provisioner.ensure_pair("r1", workspace=workspace)
        try:
            assert workspace.is_dir()
            assert fake_sbx.meta(pair.agent.name)["workspace"] == str(workspace.resolve())
        finally:
            pair.cleanup()


def make_isolation_provisioner(
    fake_sbx: FakeSbx,
    tmp_path: Path,
    workspace: Path | None,
    isolation: str = "auto",
) -> tuple[Provisioner, list[Event]]:
    sandbox: dict[str, str] = {"workspace_isolation": isolation}
    if workspace is not None:
        sandbox["workspace"] = str(workspace)
    config = Config.model_validate(
        {"state_dir": str(tmp_path / "state"), "sandbox": sandbox, **GITHUB_ENABLED}
    )
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    return make_provisioner(fake_sbx, tmp_path, config=config, bus=bus), events


def clone_events(events: list[Event]) -> list[Event]:
    return [e for e in events if e.type == "sandbox.workspace_clone"]


class TestGithubOnly:
    """The daemon's long-lived github-ops sandbox: one github-role microVM
    outside any run, same fail-fast/rollback discipline as the pair."""

    def test_creates_one_github_sandbox_by_name(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        sandbox = provisioner.ensure_github_only("sbxloop-daemon-github", tmp_path / "ws")
        try:
            assert sandbox.name == "sbxloop-daemon-github"
            created = [c[1].removeprefix("--name=") for c in fake_sbx.invocations("create")]
            assert created == ["sbxloop-daemon-github"]
            assert fake_sbx.meta(sandbox.name)["workspace"] == str((tmp_path / "ws").resolve())
            allows = fake_sbx.policies()
            assert any("uploads.github.com" in c for c in allows)
            assert not any("api.githubcopilot.com" in c for c in allows)
        finally:
            sandbox.rm()

    def test_missing_gh_token_fails_before_create(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path, env={"COPILOT_GITHUB_TOKEN": "x"})
        with pytest.raises(ProvisionError, match="GH_TOKEN"):
            provisioner.ensure_github_only("sbxloop-daemon-github", tmp_path / "ws")
        assert fake_sbx.invocations("create") == []

    def test_failure_after_create_rolls_back(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        fake_sbx.fail_next("policy allow", returncode=1, stderr="policy exploded")
        with pytest.raises(ProvisionError):
            provisioner.ensure_github_only("sbxloop-daemon-github", tmp_path / "ws")
        assert fake_sbx.invocations("rm") != []

    def test_post_create_failure_rolls_back_sandbox_and_secrets(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """The daemon installs its worker via post_create so an install
        failure gets the same sandbox+secret rollback as any provisioning
        failure (review: previously only the sandbox was removed)."""
        provisioner = make_provisioner(fake_sbx, tmp_path)

        def boom(sandbox: object, role: str) -> None:
            raise RuntimeError("worker install exploded")

        with pytest.raises(ProvisionError, match="worker install exploded"):
            provisioner.ensure_github_only(
                "sbxloop-daemon-github", tmp_path / "ws", post_create=boom
            )
        assert fake_sbx.invocations("rm") != []
        # secrets registered for the sandbox were unregistered again
        assert not any("sbxloop-daemon-github" in s for s in fake_sbx.secrets())


class TestAgentOnly:
    """The daemon's long-lived concierge sandbox: one agent-role microVM
    outside any run — Copilot token, no GH_TOKEN, prompt-advertised
    baseline allows, and the pair's rollback discipline."""

    def test_creates_one_agent_sandbox_with_copilot_secret_and_allows(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        sandbox = provisioner.ensure_agent_only("sbxloop-concierge-abcd1234", tmp_path / "ws")
        try:
            assert sandbox.name == "sbxloop-concierge-abcd1234"
            created = [c[1].removeprefix("--name=") for c in fake_sbx.invocations("create")]
            assert created == ["sbxloop-concierge-abcd1234"]
            allows = fake_sbx.policies()
            assert any("api.githubcopilot.com" in c for c in allows)
            assert any("pypi.org" in c for c in allows)
            assert not any("uploads.github.com" in c for c in allows)
            secret_args = [" ".join(s["args"]) for s in fake_sbx.secrets()]
            assert any("COPILOT_GITHUB_TOKEN" in a for a in secret_args)
            assert not any("set github" in a or "--service" in a for a in secret_args)
            # The host-tool response directory exists from the start.
            fs = fake_sbx.sandbox_fs(sandbox.name)
            assert (fs / "home/agent/.sbxloop/tools").is_dir()
        finally:
            sandbox.rm()

    def test_missing_copilot_token_fails_before_create(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path, env={"GH_TOKEN": "x"})
        with pytest.raises(ProvisionError, match="COPILOT_GITHUB_TOKEN"):
            provisioner.ensure_agent_only("sbxloop-concierge-abcd1234", tmp_path / "ws")
        assert fake_sbx.invocations("create") == []

    def test_post_create_failure_rolls_back_sandbox_and_secrets(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)

        def boom(sandbox: object, role: str) -> None:
            raise RuntimeError("worker install exploded")

        with pytest.raises(ProvisionError, match="worker install exploded"):
            provisioner.ensure_agent_only(
                "sbxloop-concierge-abcd1234", tmp_path / "ws", post_create=boom
            )
        assert fake_sbx.invocations("rm") != []
        # secrets registered for the sandbox were unregistered again
        assert fake_sbx.invocations("secret rm") != []


class TestWorkspaceIsolation:
    """Runs against a git-checkout workspace work in a per-run clone; the
    checkout's working tree and branches are never disturbed."""

    def test_auto_clones_git_workspace(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        from tests.unit.test_hostgit import make_repo

        source = make_repo(tmp_path)
        provisioner, events = make_isolation_provisioner(fake_sbx, tmp_path, source)
        clone_dir = (tmp_path / "state" / "runs" / "r1" / "workspace").resolve()

        pair = provisioner.ensure_pair("r1")
        try:
            assert pair.workspace == clone_dir
            assert (clone_dir / ".git").is_dir()
            assert (clone_dir / "hello.txt").read_text() == "hi\n"
            head = (clone_dir / ".git" / "HEAD").read_text().strip()
            assert head.endswith("refs/heads/sbxloop/r1")
            assert fake_sbx.meta(pair.agent.name)["workspace"] == str(clone_dir)
            # the source checkout is untouched
            assert not (source / ".git" / "refs" / "heads" / "sbxloop").exists()
            (event,) = clone_events(events)
            assert event.data["source"] == str(source.resolve())
            assert event.data["target"] == str(clone_dir)
            assert event.data["branch"] == "sbxloop/r1"
            assert event.data["dirty"] is False
            assert event.data["reused"] is False
            assert len(str(event.data["commit"])) == 40
        finally:
            pair.cleanup()

    def test_stray_state_dirs_do_not_trip_the_dirty_refusal(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """Running any sbxloop command from inside the checkout drops a
        relative .sbxloop there; the tool's own state (under the default
        name or this run's configured state-dir name) must be invisible to
        isolation (field failure r5a1d9m9c)."""
        from tests.unit.test_hostgit import make_repo

        source = make_repo(tmp_path)
        (source / ".sbxloop").mkdir()
        (source / ".sbxloop" / "state.db").write_text("db\n")
        (source / "state").mkdir()  # this run's state_dir is named "state"
        (source / "state" / "junk").write_text("x\n")
        provisioner, events = make_isolation_provisioner(fake_sbx, tmp_path, source)
        pair = provisioner.ensure_pair("r1")
        try:
            assert (pair.workspace / ".git").is_dir()
            (event,) = clone_events(events)
            assert event.data["dirty"] is False
        finally:
            pair.cleanup()

    def test_auto_dirty_refuses_before_provisioning(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        from tests.unit.test_hostgit import make_repo

        source = make_repo(tmp_path)
        (source / "uncommitted.txt").write_text("x\n")
        provisioner, _events = make_isolation_provisioner(fake_sbx, tmp_path, source)

        with pytest.raises(ProvisionError, match="Commit or stash"):
            provisioner.ensure_pair("r1")
        assert fake_sbx.invocations("create") == []
        assert not (tmp_path / "state" / "runs" / "r1" / "workspace").exists()

    def test_auto_non_git_workspace_stays_in_place(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        source = tmp_path / "plain-ws"
        source.mkdir()
        provisioner, events = make_isolation_provisioner(fake_sbx, tmp_path, source)
        pair = provisioner.ensure_pair("r1")
        try:
            assert pair.workspace == source.resolve()
            assert clone_events(events) == []
        finally:
            pair.cleanup()

    def test_auto_default_workspace_unchanged(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner, events = make_isolation_provisioner(fake_sbx, tmp_path, None)
        pair = provisioner.ensure_pair("r1")
        try:
            assert pair.workspace == (tmp_path / "state" / "runs" / "r1" / "workspace").resolve()
            assert clone_events(events) == []
        finally:
            pair.cleanup()

    def test_auto_subdir_of_checkout_refuses(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        from tests.unit.test_hostgit import make_repo

        source = make_repo(tmp_path)
        sub = source / "sub"
        sub.mkdir()
        provisioner, _events = make_isolation_provisioner(fake_sbx, tmp_path, sub)
        with pytest.raises(ProvisionError, match="not its root"):
            provisioner.ensure_pair("r1")

    def test_auto_unborn_head_refuses(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        import subprocess

        source = tmp_path / "empty-repo"
        source.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
        provisioner, _events = make_isolation_provisioner(fake_sbx, tmp_path, source)
        with pytest.raises(ProvisionError, match="no commits"):
            provisioner.ensure_pair("r1")

    def test_in_place_never_touches_git(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        from tests.unit.test_hostgit import make_repo

        source = make_repo(tmp_path)
        (source / "uncommitted.txt").write_text("x\n")  # dirty is fine in-place
        provisioner, events = make_isolation_provisioner(
            fake_sbx, tmp_path, source, isolation="in-place"
        )
        pair = provisioner.ensure_pair("r1")
        try:
            assert pair.workspace == source.resolve()
            assert clone_events(events) == []
        finally:
            pair.cleanup()

    def test_clone_mode_dirty_proceeds_from_head(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        from tests.unit.test_hostgit import make_repo

        source = make_repo(tmp_path)
        (source / "uncommitted.txt").write_text("x\n")
        provisioner, events = make_isolation_provisioner(
            fake_sbx, tmp_path, source, isolation="clone"
        )
        pair = provisioner.ensure_pair("r1")
        try:
            assert not (pair.workspace / "uncommitted.txt").exists()
            (event,) = clone_events(events)
            assert event.data["dirty"] is True
            assert "NOT in the run workspace" in event.data["message"]
        finally:
            pair.cleanup()

    def test_clone_mode_non_git_errors(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        source = tmp_path / "plain"
        source.mkdir()
        provisioner, _events = make_isolation_provisioner(
            fake_sbx, tmp_path, source, isolation="clone"
        )
        with pytest.raises(ProvisionError, match="not a git repository"):
            provisioner.ensure_pair("r1")

    def test_clone_mode_without_workspace_errors(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner, _events = make_isolation_provisioner(
            fake_sbx, tmp_path, None, isolation="clone"
        )
        with pytest.raises(ProvisionError, match="requires \\[sandbox\\] workspace"):
            provisioner.ensure_pair("r1")

    def test_existing_clone_reused_not_recloned(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        """Crash-before-pin resume re-enters isolation with no workspace arg;
        the run's existing clone (and any agent work in it) must survive."""
        from tests.unit.test_hostgit import make_repo

        source = make_repo(tmp_path)
        provisioner, events = make_isolation_provisioner(fake_sbx, tmp_path, source)
        pair = provisioner.ensure_pair("r1")
        pair.cleanup()
        sentinel = pair.workspace / "agent-work.txt"
        sentinel.write_text("precious\n")

        pair2 = provisioner.ensure_pair("r1")
        try:
            assert pair2.workspace == pair.workspace
            assert sentinel.read_text() == "precious\n"
            first, second = clone_events(events)
            assert first.data["reused"] is False
            assert second.data["reused"] is True
        finally:
            pair2.cleanup()

    def test_workspace_arg_bypasses_isolation(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        """The explicit arg is the resume pin: it must be used in place even
        when it is a git checkout and isolation is on."""
        from tests.unit.test_hostgit import make_repo

        source = make_repo(tmp_path)
        provisioner, events = make_isolation_provisioner(fake_sbx, tmp_path, None)
        pair = provisioner.ensure_pair("r1", workspace=source)
        try:
            assert pair.workspace == source.resolve()
            assert clone_events(events) == []
        finally:
            pair.cleanup()

    def test_auto_without_git_binary_falls_back_in_place(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sbxloop.hostgit as hostgit_mod
        from tests.unit.test_hostgit import make_repo

        source = make_repo(tmp_path)
        monkeypatch.setattr(hostgit_mod, "find_git", lambda: None)
        provisioner, events = make_isolation_provisioner(fake_sbx, tmp_path, source)
        pair = provisioner.ensure_pair("r1")
        try:
            assert pair.workspace == source.resolve()
            assert clone_events(events) == []
        finally:
            pair.cleanup()


class TestSecretIdempotency:
    def secret_state(self, fake_sbx: FakeSbx) -> dict[str, dict[str, str]]:
        import json

        path = fake_sbx.state / "secrets-state.json"
        return json.loads(path.read_text()) if path.is_file() else {"service": {}, "custom": {}}

    def test_cleanup_unregisters_the_pair_secrets(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        """``sbx rm`` leaves the sandbox-scoped registrations behind; on db
        every run leaked one COPILOT_GITHUB_TOKEN and one github entry
        until pair cleanup started removing them (field failure
        rgn9ccjam)."""
        provisioner = make_provisioner(fake_sbx, tmp_path)
        pair = provisioner.ensure_pair("r1")
        state = self.secret_state(fake_sbx)
        assert "COPILOT_GITHUB_TOKEN" in state["custom"]
        assert any(k.startswith("sbxloop-r1-github|") for k in state["service"])
        pair.cleanup()
        state = self.secret_state(fake_sbx)
        assert state["custom"] == {}
        assert not any(k.startswith("sbxloop-r1-") for k in state["service"])

    def test_reprovision_replaces_secret_owned_by_old_scope(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """Second run, field-reported shape: sbx keys custom secrets by env
        name and the conflicting entry is owned by the PREVIOUS run's scope
        ('already exists in scope <old> with placeholder ...'). The
        provisioner must parse that scope out of the error, remove the old
        entry there, and re-set — never die."""
        provisioner = make_provisioner(fake_sbx, tmp_path)
        provisioner.ensure_pair("r1").cleanup()
        pair = provisioner.ensure_pair("r2")  # must not raise
        try:
            state = self.secret_state(fake_sbx)
            entry = state["custom"]["COPILOT_GITHUB_TOKEN"]
            assert entry["scope"] == "sbxloop-r2-agent"  # replaced, new owner
            assert entry["value"] == "github_pat_copilot"
            # the rm targeted the OLD run's scope, parsed from the error
            rms = [s["args"] for s in fake_sbx.secrets() if s["args"][0] == "rm"]
            assert any(
                "--sandbox" in a and a[a.index("--sandbox") + 1] == "sbxloop-r1-agent" for a in rms
            )
            # every removal is forced: without -f sbx 0.38 prompts, cancels
            # non-interactively, and exits 0 having removed nothing
            assert all("-f" in a for a in rms)
        finally:
            pair.cleanup()

    def test_resume_same_run_id_replaces_service_secret(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """resume() reuses run ids -> same sandbox names -> the github
        service secret collides too; it must be replaced with the current
        token value."""
        provisioner = make_provisioner(fake_sbx, tmp_path)
        provisioner.ensure_pair("r1").cleanup()
        rotated = dict(TOKENS, GH_TOKEN="github_pat_rotated")
        provisioner2 = make_provisioner(fake_sbx, tmp_path, env=rotated)
        pair = provisioner2.ensure_pair("r1")
        try:
            state = self.secret_state(fake_sbx)
            assert state["service"]["sbxloop-r1-github|github"] == "github_pat_rotated"
        finally:
            pair.cleanup()

    def test_unremovable_secret_keeps_existing_value_with_warning(
        self, fake_sbx: FakeSbx, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        first = provisioner.ensure_pair("r1")
        # every rm is rejected (simulating an sbx build without custom rm):
        # r1's cleanup then leaves its registrations behind, and r2 must
        # cope with the collision without dying
        fake_sbx.script("secret rm", returncode=1, stderr="unknown command")
        first.cleanup()
        import logging

        with caplog.at_level(logging.WARNING):
            pair = provisioner.ensure_pair("r2")  # still must not raise
        pair.cleanup()
        assert any("could not be replaced" in r.message for r in caplog.records)

    def test_non_exists_secret_error_still_raises(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        fake_sbx.script("secret set-custom", returncode=1, stderr="keychain locked")
        with pytest.raises(ProvisionError):
            provisioner.ensure_pair("r1")


class TestSecretEnvVerification:
    def test_missing_env_auto_heals_with_plain_env_fallback(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Field-confirmed: sbx proxy secrets never reach exec'd processes.
        Under the proxy strategy, an invisible token must trigger the
        plain-env fallback for that sandbox so the run still works."""
        # the fake exec inherits the test process env; ensure the tokens are
        # NOT visible inside the sandbox shells
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)
        pair = provisioner.ensure_pair("r1")
        try:
            fallback = [e for e in events if e.type == "sandbox.secret_env_fallback"]
            # parallel provisioning: one fallback per sandbox, any order
            assert sorted(e.data["env"] for e in fallback) == [
                "COPILOT_GITHUB_TOKEN",
                "GH_TOKEN",
            ]
            # one concise single-line message, not a paragraph
            assert "plain-env" in fallback[0].data["message"]
            assert "\n" not in fallback[0].data["message"]
            assert len(fallback[0].data["message"]) < 160
            # the in-VM env file now carries the tokens the worker will load
            agent_env = (
                fake_sbx.sandbox_fs(pair.agent.name) / "home/agent/.sbxloop/env.sh"
            ).read_text()
            github_env = (
                fake_sbx.sandbox_fs(pair.github.name) / "home/agent/.sbxloop/env.sh"
            ).read_text()
            assert "export COPILOT_GITHUB_TOKEN=github_pat_copilot" in agent_env
            assert "export GH_TOKEN=github_pat_user" in github_env
        finally:
            pair.cleanup()

    def test_visible_env_emits_nothing(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "github_pat_x")
        monkeypatch.setenv("GH_TOKEN", "github_pat_y")
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)
        pair = provisioner.ensure_pair("r1")
        try:
            assert not [e for e in events if "secret_env" in e.type]
        finally:
            pair.cleanup()

    def test_probe_infra_failure_never_downgrades_to_plain_env(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #63 acceptance: an exec-level failure during the secret
        visibility probe must fail provisioning loudly — never silently
        select the weaker plain-env strategy."""
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        # every probe attempt (including the retry) dies at the sbx level
        fake_sbx.script(
            "exec sbxloop-r1-agent sh -lc test -n",
            returncode=1,
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        )
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)
        with pytest.raises(ProvisionError, match="refusing to auto-downgrade"):
            provisioner.ensure_pair("r1")
        # no plain-env fallback FOR THE FAILING SANDBOX: distinct
        # probe-error event, not the fallback event a clean "invisible"
        # answer produces (the github sandbox provisions in parallel and
        # its own clean answer may legitimately fall back)
        fallback = [e for e in events if e.type == "sandbox.secret_env_fallback"]
        assert not [e for e in fallback if e.data["env"] == "COPILOT_GITHUB_TOKEN"]
        errors = [e for e in events if e.type == "sandbox.secret_probe_error"]
        assert [e.data["env"] for e in errors] == ["COPILOT_GITHUB_TOKEN"]
        # rollback removed everything the attempt created — including the
        # github sandbox whose own provisioning succeeded
        assert not (fake_sbx.state / "sandboxes" / "sbxloop-r1-agent").exists()
        assert not (fake_sbx.state / "sandboxes" / "sbxloop-r1-github").exists()

    def test_transient_probe_error_retries_to_a_clean_answer(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        # first probe attempt hits a transient sbx failure; the retry gets
        # the real fake's clean "invisible" answer -> normal fallback
        fake_sbx.fail_next(
            "exec sbxloop-r1-agent sh -lc test -n",
            returncode=1,
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        )
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)
        pair = provisioner.ensure_pair("r1")
        try:
            fallback = [e for e in events if e.type == "sandbox.secret_env_fallback"]
            assert sorted(e.data["env"] for e in fallback) == [
                "COPILOT_GITHUB_TOKEN",
                "GH_TOKEN",
            ]
            assert not [e for e in events if e.type == "sandbox.secret_probe_error"]
        finally:
            pair.cleanup()

    def test_ambiguous_probe_exit_code_fails_loudly(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # `test -n` answers with 0 or 1; anything else is not an answer
        fake_sbx.script("exec sbxloop-r1-agent sh -lc test -n", returncode=3)
        provisioner = make_provisioner(fake_sbx, tmp_path)
        with pytest.raises(ProvisionError, match="without a clean answer"):
            provisioner.ensure_pair("r1")

    def test_plain_env_strategy_skips_verification(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit plain-env: the worker loads the env file itself, so the
        shell visibility check (which can never pass) must not run or warn."""
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        config = Config.model_validate(
            {"secret_strategy": "plain-env", "state_dir": str(tmp_path / "state"), **GITHUB_ENABLED}
        )
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, config=config, bus=bus)
        pair = provisioner.ensure_pair("r1")
        try:
            assert not [e for e in events if "secret_env" in e.type]
            checks = [c for c in fake_sbx.invocations("exec") if any("test -n" in a for a in c)]
            assert checks == []
        finally:
            pair.cleanup()


class TestMountDiscovery:
    def test_mounted_workspace_discovered(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)
        pair = provisioner.ensure_pair("r1")
        try:
            assert pair.mounted
            assert pair.workspace == (tmp_path / "state/runs/r1/workspace").resolve()
            # the discovered in-VM dir IS the host workspace (fake models the
            # mount as a symlink: writes propagate live)
            assert Path(pair.agent_workdir).resolve() == pair.workspace.resolve()
            (Path(pair.agent_workdir) / "probe.txt").write_text("via mount")
            assert (pair.workspace / "probe.txt").read_text() == "via mount"
            # nonce marker cleaned up
            assert not list(pair.workspace.glob(".sbxloop-mount-*"))
            mount_events = [e for e in events if e.type == "sandbox.workspace_mount"]
            assert [e.data["mounted"] for e in mount_events] == [True]
            assert mount_events[0].data["path"] == pair.agent_workdir
        finally:
            pair.cleanup()

    def test_discovery_failure_falls_back_to_harvest_dir(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SBX_FAKE_NO_MOUNT", "1")
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)
        pair = provisioner.ensure_pair("r1")
        try:
            assert not pair.mounted
            assert pair.agent_workdir == WORK_DIR
            # fallback work dir created inside the VM
            assert (fake_sbx.sandbox_fs(pair.agent.name) / "home/agent/work").is_dir()
            assert not list(pair.workspace.glob(".sbxloop-mount-*"))
            mount_events = [e for e in events if e.type == "sandbox.workspace_mount"]
            assert [e.data["mounted"] for e in mount_events] == [False]
            # a clean negative answer, distinguishable from a broken probe
            assert mount_events[0].data["probe"] == "answered"
        finally:
            pair.cleanup()

    def test_discovery_exec_error_is_non_fatal_but_distinguishable(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        from sbxloop.sbx.conformance import PROBE_WORKSPACE_MOUNT, load_verdicts

        # sbx-level failure of the find probe must degrade, not abort the run
        fake_sbx.script("exec sbxloop-r1-agent sh -c set --", returncode=1, stderr="not found")
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)
        pair = provisioner.ensure_pair("r1")
        try:
            assert not pair.mounted
            assert pair.agent_workdir == WORK_DIR
            # the event says the probe FAILED — not that sbx answered "no
            # mount" — so field debugging chases the right cause (#63)
            mount_events = [e for e in events if e.type == "sandbox.workspace_mount"]
            assert [e.data["probe"] for e in mount_events] == ["error"]
            # and the infra failure did not clobber the conformance cache
            # with a bogus "not-found" verdict
            assert PROBE_WORKSPACE_MOUNT not in load_verdicts(tmp_path / "state", "0.38.0")
        finally:
            pair.cleanup()


class TestConformanceRecording:
    """Provisioning's own field checks double as conformance probes: every
    run refreshes the version-keyed verdict cache for free."""

    def test_ensure_pair_records_field_verdicts(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        from sbxloop.sbx.conformance import (
            PROBE_SECRET_ENV_VISIBILITY,
            PROBE_WORKSPACE_MOUNT,
            load_verdicts,
        )

        provisioner = make_provisioner(fake_sbx, tmp_path)
        pair = provisioner.ensure_pair("r1")
        try:
            cached = load_verdicts(tmp_path / "state", "0.38.0")
            assert cached[PROBE_SECRET_ENV_VISIBILITY].verdict == "invisible-under-exec"
            assert cached[PROBE_SECRET_ENV_VISIBILITY].source == "provision"
            assert cached[PROBE_WORKSPACE_MOUNT].verdict == "discoverable"
            assert cached[PROBE_WORKSPACE_MOUNT].source == "provision"
        finally:
            pair.cleanup()

    def test_recording_failure_never_breaks_provisioning(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.sbx import provision as provision_module

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(provision_module, "record_field_verdict", boom)
        provisioner = make_provisioner(fake_sbx, tmp_path)
        pair = provisioner.ensure_pair("r1")
        try:
            assert pair.mounted
        finally:
            pair.cleanup()
