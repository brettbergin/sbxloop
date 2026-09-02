"""Provisioner tests: specs, token split, policy, secrets, rollback."""

import shutil
from pathlib import Path
from typing import ClassVar

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


@pytest.fixture(autouse=True)
def _no_ambient_github_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """FakeSbx execs run on the host, so probe answers (secret visibility,
    the #576 shadow probe) would vary with the developer's or CI's exported
    tokens; every test here passes credentials explicitly via ``env=``."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


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

    def test_selected_toolchain_installer_hosts_are_seeded(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # #616: provisioning runs before any plan, so a toolchain's
        # installer host has to be on the spec — for the selected
        # toolchains only.
        provisioner = make_provisioner(fake_sbx, tmp_path)
        agent, github = provisioner.build_specs("r1", tmp_path, languages=["go", "php"])
        for host in ("go.dev", "dl.google.com", "getcomposer.org"):
            assert host in agent.policy_allows, host
            assert host not in github.policy_allows, host
        assert "nodejs.org" not in agent.policy_allows
        assert "static.rust-lang.org" not in agent.policy_allows

    def test_build_specs_without_languages_uses_the_config(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        config = Config.model_validate({"sandbox": {"languages": ["rust"]}, **GITHUB_ENABLED})
        provisioner = make_provisioner(fake_sbx, tmp_path, config=config)
        agent, _github = provisioner.build_specs("r1", tmp_path)
        assert "static.rust-lang.org" in agent.policy_allows

    def test_denied_installer_host_is_never_seeded(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        config = Config.model_validate({"policy": {"deny": ["*.rust-lang.org"]}, **GITHUB_ENABLED})
        provisioner = make_provisioner(fake_sbx, tmp_path, config=config)
        agent, _github = provisioner.build_specs("r1", tmp_path, languages=["rust", "go"])
        assert "static.rust-lang.org" not in agent.policy_allows
        assert "go.dev" in agent.policy_allows

    def test_claude_backend_seeds_the_cli_runtime_host(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # The claude backend's in-sandbox CLI comes from npm, which is in
        # the baseline already — the point is that the entry is consulted.
        config = Config.model_validate({"agent": {"backend": "claude"}, **GITHUB_ENABLED})
        provisioner = make_provisioner(
            fake_sbx, tmp_path, config=config, env={**TOKENS, "ANTHROPIC_API_KEY": "sk"}
        )
        agent, _github = provisioner.build_specs("r1", tmp_path, languages=["python"])
        assert "registry.npmjs.org" in agent.policy_allows

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

    def test_ensure_pair_resolves_languages_from_the_workspace(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # #624: decided once, after the workspace exists and before any
        # microVM does; reported as an event; carried on the pair.
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "Cargo.toml").write_text("[package]\n")
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)
        pair = provisioner.ensure_pair("r1", workspace)
        try:
            assert pair.languages.languages == ("rust",)
            assert pair.languages.source == "detected"
            langs = [e for e in events if e.type == "sandbox.languages"]
            assert len(langs) == 1
            assert langs[0].data == {
                "languages": ["rust"],
                "source": "detected",
                "signals": {"rust": ["Cargo.toml"]},
            }
            # the event precedes sandbox creation: the allowlist depends on it
            types = [e.type for e in events]
            assert types.index("sandbox.languages") < types.index("sandbox.provision_start")
            assert [
                "allow",
                "network",
                "static.rust-lang.org",
                "--sandbox",
                pair.agent.name,
            ] in fake_sbx.policies()
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
            # no sbx secret registrations under plain-env (the #576 purge's
            # best-effort `secret rm` calls are expected)
            sets = [s for s in fake_sbx.secrets() if s["args"][0] in ("set", "set-custom")]
            assert sets == []
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
            "exec sbxloop-r1-github sh -lc v=",
            returncode=1,
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        )
        with pytest.raises(ProvisionError, match="probe failed twice"):
            provisioner.ensure_pair("r1")
        assert self.registered_custom_secrets(fake_sbx) == {}
        state = json.loads((fake_sbx.state / "secrets-state.json").read_text())
        assert state["service"] == {}
        # a fresh attempt provisions without needing any collision recovery
        # r1's agent probe cached "invisible-under-exec"; clear it so r2
        # takes the registration path whose no-recovery property this
        # asserts (the cached path is covered by TestCachedProxyVerdictSkip).
        shutil.rmtree(tmp_path / "state" / "conformance")
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

    def test_the_clone_branch_carries_the_operators_prefix(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """#621: `[github] branch_prefix` names the run branch everywhere it
        is minted; the clone is the first place."""
        from tests.unit.test_hostgit import make_repo

        source = make_repo(tmp_path)
        provisioner, events = make_isolation_provisioner(fake_sbx, tmp_path, source)
        provisioner.config.github.branch_prefix = "bot/"
        pair = provisioner.ensure_pair("r1")
        try:
            head = (pair.workspace / ".git" / "HEAD").read_text().strip()
            assert head.endswith("refs/heads/bot/r1")
            (event,) = clone_events(events)
            assert event.data["branch"] == "bot/r1"
        finally:
            pair.cleanup()

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
        # r1's probe cached "invisible-under-exec"; clear it so r2 takes the
        # registration path whose collision recovery this test exercises.
        shutil.rmtree(tmp_path / "state" / "conformance")
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
        # r1's probe cached "invisible-under-exec", which would send the
        # resume straight to the env file (see TestCachedProxyVerdictSkip's
        # rotation test); clear it so this test keeps exercising the
        # registration-collision replacement an unknown sbx version takes.
        shutil.rmtree(tmp_path / "state" / "conformance")
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
        # r1's probe cached "invisible-under-exec"; clear it so r2 takes the
        # registration path this test exists to exercise.
        shutil.rmtree(tmp_path / "state" / "conformance")
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
            "exec sbxloop-r1-agent sh -lc v=",
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
            "exec sbxloop-r1-agent sh -lc v=",
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

    def test_a_proxy_sentinel_under_exec_falls_back_like_an_absent_one(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Field failure 2026-08-21: sbx exported its proxy sentinel into the
        exec environment. `test -n` called that "visible" and skipped the
        fallback, so the agent got a token-shaped hole and every session died
        with a 401. A sentinel is not a credential — fall back."""
        # The fake runs exec for real, so the host env IS the sandbox env.
        # Both roles get a sentinel: the pair provisions in parallel and both
        # probes write the same verdict key, so mixing answers would make the
        # cached value a coin toss (the same race behind the long-standing
        # flake in test_ensure_pair_records_field_verdicts).
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "sbx-cs-Xrz8X47IcldsQVJ0")
        monkeypatch.setenv("GH_TOKEN", "sbx-cs-Xrz8X47IcldsQVJ0")
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)
        pair = provisioner.ensure_pair("r1")
        try:
            fallback = [
                e
                for e in events
                if e.type == "sandbox.secret_env_fallback"
                and e.data["env"] == "COPILOT_GITHUB_TOKEN"
            ]
            assert fallback, "a sentinel must trigger the in-VM env-file fallback"
            assert "sentinel" in fallback[0].data["message"]
            assert not [e for e in events if e.type == "sandbox.secret_probe_error"]
            from sbxloop.sbx.conformance import PROBE_SECRET_ENV_VISIBILITY, load_verdicts

            cached = load_verdicts(tmp_path / "state", "0.38.0")
            assert cached[PROBE_SECRET_ENV_VISIBILITY].verdict == "sentinel-under-exec"
        finally:
            pair.cleanup()

    def test_a_real_token_under_exec_keeps_it_out_of_the_vm(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other side of the same branch: when the proxy really does
        deliver the value, there is nothing to heal and no env file."""
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_realtokenshapedvalue")
        monkeypatch.setenv("GH_TOKEN", "gho_realtokenshapedvalue")
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)
        pair = provisioner.ensure_pair("r1")
        try:
            assert not [e for e in events if e.type == "sandbox.secret_env_fallback"]
            from sbxloop.sbx.conformance import PROBE_SECRET_ENV_VISIBILITY, load_verdicts

            cached = load_verdicts(tmp_path / "state", "0.38.0")
            assert cached[PROBE_SECRET_ENV_VISIBILITY].verdict == "visible-under-exec"
        finally:
            pair.cleanup()

    def test_ambiguous_probe_exit_code_fails_loudly(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        # the probe answers 0 (usable), 1 (unset) or 3 (sentinel); anything
        # else is not an answer
        fake_sbx.script("exec sbxloop-r1-agent sh -lc v=", returncode=5)
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
            checks = [c for c in fake_sbx.invocations("exec") if any('v="$' in a for a in c)]
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

    def test_ensure_pair_records_field_verdicts(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.sbx.conformance import (
            PROBE_SECRET_ENV_VISIBILITY,
            PROBE_WORKSPACE_MOUNT,
            load_verdicts,
        )

        # The fake runs exec for real, so the host env IS the sandbox env, and
        # the pair provisions in parallel with both roles writing the same
        # verdict key. Unless both probes get the same answer the cached
        # verdict is whichever role finished last — a coin toss.
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
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


class TestGithubAppAuth:
    """GitHub App installation credentials (#568): env-file delivery, the
    PAT-vs-App decision, and the live-sandbox refresh hook."""

    APP_ENV: ClassVar[dict[str, str]] = {
        "COPILOT_GITHUB_TOKEN": "github_pat_copilot",
        "GITHUB_APP_ID": "12345",
        "GITHUB_APP_INSTALLATION_ID": "678",
        "GITHUB_APP_PRIVATE_KEY": (
            "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----"
        ),
    }

    def stub_mint(
        self, monkeypatch: pytest.MonkeyPatch, *, lifetime_s: float = 3600.0
    ) -> list[str]:
        """Never mint over the network in unit tests."""
        import time as _time

        from sbxloop.gh import appauth

        minted: list[str] = []

        def mint(creds: object, **kwargs: object) -> appauth.InstallationToken:
            minted.append(f"ghs_minted{len(minted) + 1}")
            return appauth.InstallationToken(minted[-1], _time.time() + lifetime_s)

        monkeypatch.setattr(appauth, "mint_installation_token", mint)
        return minted

    def github_env_sh(self, fake_sbx: FakeSbx, name: str) -> str:
        return (fake_sbx.sandbox_fs(name) / "home/agent/.sbxloop/env.sh").read_text()

    def test_app_mode_writes_env_file_and_registers_no_service_secret(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        minted = self.stub_mint(monkeypatch)
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, env=self.APP_ENV, bus=bus)
        pair = provisioner.ensure_pair("r1")
        try:
            assert minted == ["ghs_minted1"]
            env_sh = self.github_env_sh(fake_sbx, pair.github.name)
            assert "export GH_TOKEN=ghs_minted1" in env_sh
            assert "export GITHUB_TOKEN=ghs_minted1" in env_sh
            # no sbx `github` service registration: each ~hourly token would
            # only be ceremony — the env file is the delivery channel
            service = [s for s in fake_sbx.secrets() if s["args"][0] == "set"]
            assert service == []
            announced = [e for e in events if e.type == "sandbox.github_app_auth"]
            assert len(announced) == 1
            assert announced[0].data["name"] == pair.github.name
        finally:
            pair.cleanup()

    def test_conflict_with_pat_is_a_named_startup_error(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        provisioner = make_provisioner(
            fake_sbx, tmp_path, env={**self.APP_ENV, "GH_TOKEN": "github_pat_user"}
        )
        with pytest.raises(ProvisionError, match="unset the PAT"):
            provisioner.ensure_pair("r1")
        assert fake_sbx.invocations("create") == []

    def test_partial_app_credentials_fail_before_any_microvm(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        env = {"COPILOT_GITHUB_TOKEN": "github_pat_copilot", "GITHUB_APP_ID": "12345"}
        provisioner = make_provisioner(fake_sbx, tmp_path, env=env)
        with pytest.raises(ProvisionError, match="incomplete GitHub App credentials"):
            provisioner.ensure_pair("r1")
        assert fake_sbx.invocations("create") == []

    def test_missing_everything_names_both_credential_options(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        provisioner = make_provisioner(
            fake_sbx, tmp_path, env={"COPILOT_GITHUB_TOKEN": "github_pat_copilot"}
        )
        with pytest.raises(ProvisionError, match="GitHub App"):
            provisioner.ensure_pair("r1")

    def test_repo_token_env_stays_an_explicit_pat_choice(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A [[github.repos]] token_env wins over ambient App credentials."""
        from sbxloop.sbx.provision import GhPat

        config = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repos": [{"repo": "owner/repo", "token_env": "GH_TOKEN_TWO"}]},
            }
        )
        provisioner = make_provisioner(
            fake_sbx,
            tmp_path,
            env={**self.APP_ENV, "GH_TOKEN_TWO": "github_pat_two"},
            config=config,
        )
        cred = provisioner.gh_credential("owner/repo")
        assert cred == GhPat("github_pat_two")

    def test_refresher_rewrites_env_file_only_when_stale(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time as _time

        from sbxloop.gh.appauth import InstallationToken

        minted = self.stub_mint(monkeypatch)
        provisioner = make_provisioner(fake_sbx, tmp_path, env=self.APP_ENV)
        sandbox = provisioner.ensure_github_only("boxg", tmp_path / "ws")
        try:
            refresher = provisioner.gh_refresher(sandbox, None)
            assert refresher is not None
            # fresh token: the hook is a no-op
            refresher()
            assert minted == ["ghs_minted1"]
            assert "ghs_minted1" in self.github_env_sh(fake_sbx, "boxg")
            # age the cached token into the refresh margin
            source = provisioner._app_source
            assert source is not None
            source._token = InstallationToken("ghs_stale", _time.time() + 60.0)
            refresher()
            env_sh = self.github_env_sh(fake_sbx, "boxg")
            assert "ghs_minted2" in env_sh
            assert "ghs_stale" not in env_sh
        finally:
            sandbox.rm()

    def test_refresher_is_none_for_pat(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        sandbox = provisioner.ensure_github_only("boxp", tmp_path / "ws")
        try:
            assert provisioner.gh_refresher(sandbox, None) is None
        finally:
            sandbox.rm()


class TestCachedProxyVerdictSkip:
    """A cached invisible/sentinel-under-exec conformance verdict sends
    provisioning straight to the env file: no doomed registration, no
    probe, no per-run warning (#568)."""

    def seed_broken_verdict(self, tmp_path: Path, verdict: str = "invisible-under-exec") -> None:
        from sbxloop.sbx.conformance import PROBE_SECRET_ENV_VISIBILITY, record_field_verdict

        record_field_verdict(tmp_path / "state", "0.38.0", PROBE_SECRET_ENV_VISIBILITY, verdict)

    def test_cached_broken_verdict_skips_registration_and_probe(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        self.seed_broken_verdict(tmp_path)
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)
        pair = provisioner.ensure_pair("r1")
        try:
            # no secret registrations at all — both roles go env-file
            # direct (the purge's best-effort `secret rm` calls are fine)
            sets = [s for s in fake_sbx.secrets() if s["args"][0] in ("set", "set-custom")]
            assert sets == []
            for name in (pair.agent.name, pair.github.name):
                env_sh = (fake_sbx.sandbox_fs(name) / "home/agent/.sbxloop/env.sh").read_text()
                assert "export" in env_sh
            fallback = [e for e in events if e.type == "sandbox.secret_env_fallback"]
            assert len(fallback) == 2
            assert all(e.data.get("cached") for e in fallback)
            assert not [e for e in events if e.type == "sandbox.secret_probe_error"]
        finally:
            pair.cleanup()

    def test_resume_under_cached_verdict_rotates_via_env_file(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """The guarantee the registration path gives a resumed run id (same
        sandbox names, current token value) is preserved by the env-file
        path: the file is simply rewritten with the fresh token."""
        self.seed_broken_verdict(tmp_path)
        make_provisioner(fake_sbx, tmp_path).ensure_pair("r1").cleanup()
        rotated = dict(TOKENS, GH_TOKEN="github_pat_rotated")
        pair = make_provisioner(fake_sbx, tmp_path, env=rotated).ensure_pair("r1")
        try:
            env_sh = (
                fake_sbx.sandbox_fs(pair.github.name) / "home/agent/.sbxloop/env.sh"
            ).read_text()
            assert "github_pat_rotated" in env_sh
            # no new registrations (r1 cleanup's best-effort rm calls are fine)
            sets = [s for s in fake_sbx.secrets() if s["args"][0] in ("set", "set-custom")]
            assert sets == []
        finally:
            pair.cleanup()

    def test_unknown_version_probes_exactly_as_before(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No cached verdict: the register→probe path runs unchanged, so a
        new sbx version still gets its one field probe per version."""
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)
        pair = provisioner.ensure_pair("r1")
        try:
            service = [s for s in fake_sbx.secrets() if s["args"][0] == "set"]
            assert len(service) == 1  # the github service secret registered
            fallback = [e for e in events if e.type == "sandbox.secret_env_fallback"]
            assert fallback and not any(e.data.get("cached") for e in fallback)
        finally:
            pair.cleanup()


class TestGhCredentialStatus:
    """The advisory twin of gh_credential, used by doctor rows."""

    def test_pat(self) -> None:
        from sbxloop.sbx.provision import gh_credential_status

        status = gh_credential_status({"GH_TOKEN": "github_pat_x"})
        assert (status.ok, status.mode) == (True, "pat")

    def test_app(self) -> None:
        from sbxloop.sbx.provision import gh_credential_status

        status = gh_credential_status(TestGithubAppAuth.APP_ENV)
        assert (status.ok, status.mode) == (True, "app")
        assert "12345" in status.detail

    def test_conflict(self) -> None:
        from sbxloop.sbx.provision import gh_credential_status

        status = gh_credential_status({**TestGithubAppAuth.APP_ENV, "GH_TOKEN": "github_pat_x"})
        assert not status.ok
        assert "both" in status.detail

    def test_partial_app_set(self) -> None:
        from sbxloop.sbx.provision import gh_credential_status

        status = gh_credential_status({"GITHUB_APP_ID": "12345"})
        assert not status.ok
        assert "incomplete" in status.detail

    def test_neither(self) -> None:
        from sbxloop.sbx.provision import gh_credential_status

        status = gh_credential_status({})
        assert (status.ok, status.mode) == (False, "none")

    def test_token_env_wins(self) -> None:
        from sbxloop.sbx.provision import gh_credential_status

        env = {**TestGithubAppAuth.APP_ENV, "GH_TOKEN_TWO": "github_pat_two"}
        status = gh_credential_status(env, token_env="GH_TOKEN_TWO")
        assert (status.ok, status.mode) == (True, "pat")
        assert "GH_TOKEN_TWO" in status.detail


class TestPurgeStaleRegistrations:
    """Env-file deliveries purge registrations parked at the sandbox name
    BEFORE create, and a github box refuses to run shadowed (#576)."""

    def stale_github_registration(self, fake_sbx: FakeSbx, scope: str) -> None:
        SbxCLI(binary=str(fake_sbx.binary)).secret_set(
            "github", sandbox=scope, token="github_pat_stale"
        )

    def indices(self, fake_sbx: FakeSbx, name: str) -> tuple[int | None, int]:
        calls = fake_sbx.invocations()
        rm = next(
            (i for i, c in enumerate(calls) if c[:2] == ["secret", "rm"] and name in c),
            None,
        )
        create = next(i for i, c in enumerate(calls) if c[0] == "create" and f"--name={name}" in c)
        return rm, create

    def test_app_mode_purges_stale_registration_before_create(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        TestGithubAppAuth().stub_mint(monkeypatch)
        self.stale_github_registration(fake_sbx, "sbxloop-r1-github")
        provisioner = make_provisioner(fake_sbx, tmp_path, env=TestGithubAppAuth.APP_ENV)
        pair = provisioner.ensure_pair("r1")
        try:
            rm, create = self.indices(fake_sbx, "sbxloop-r1-github")
            assert rm is not None and rm < create
            import json

            state_path = fake_sbx.state / "secrets-state.json"
            state = json.loads(state_path.read_text())
            assert "sbxloop-r1-github|github" not in state.get("service", {})
        finally:
            pair.cleanup()

    def test_cached_verdict_mode_purges_too(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        TestCachedProxyVerdictSkip().seed_broken_verdict(tmp_path)
        self.stale_github_registration(fake_sbx, "sbxloop-r1-github")
        provisioner = make_provisioner(fake_sbx, tmp_path)
        pair = provisioner.ensure_pair("r1")
        try:
            rm, create = self.indices(fake_sbx, "sbxloop-r1-github")
            assert rm is not None and rm < create
        finally:
            pair.cleanup()

    def test_proxy_mode_does_not_purge_before_create(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """The proxy path replaces in place (set_secret_replacing) after
        create, exactly as before — no new rm ahead of it."""
        provisioner = make_provisioner(fake_sbx, tmp_path)
        pair = provisioner.ensure_pair("r1")
        try:
            rm, create = self.indices(fake_sbx, "sbxloop-r1-github")
            assert rm is None or rm > create
        finally:
            pair.cleanup()

    def test_shadowing_credential_fails_provisioning_loudly(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A credential-shaped GH_TOKEN visible to exec (a registration the
        purge cannot reach, e.g. global scope) must refuse the box rather
        than run as the wrong identity."""
        TestGithubAppAuth().stub_mint(monkeypatch)
        monkeypatch.setenv("GH_TOKEN", "gho_shape_mimicking_sentinel")
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, env=TestGithubAppAuth.APP_ENV, bus=bus)
        with pytest.raises(ProvisionError, match="wrong identity"):
            provisioner.ensure_pair("r1")
        shadowed = [e for e in events if e.type == "sandbox.credential_shadowed"]
        assert len(shadowed) == 1
        # rollback ran: the half-provisioned pair is gone
        assert fake_sbx.invocations("rm") != []

    def test_plain_sbx_sentinel_is_not_a_shadow(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An sbx-cs- placeholder is overridden by the worker's env-file
        load, so it must not trip the probe."""
        TestGithubAppAuth().stub_mint(monkeypatch)
        monkeypatch.setenv("GH_TOKEN", "sbx-cs-abcdef123456")
        provisioner = make_provisioner(fake_sbx, tmp_path, env=TestGithubAppAuth.APP_ENV)
        pair = provisioner.ensure_pair("r1")
        pair.cleanup()


class TestMimicSentinels:
    """sbx's shape-mimicking service placeholders (gho_sbxproxymanaged…)
    classify as sentinels in both probes (#576 follow-up)."""

    MIMIC = "gho_sbxproxymanagedAbc123"

    def test_visibility_probe_treats_mimic_as_sentinel(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proxy strategy + a mimic visible under exec: that is the
        sentinel outcome — fall back to the env file and cache
        sentinel-under-exec, never 'visible' (db cached the wrong verdict
        for 18h on exactly this)."""
        from sbxloop.sbx.conformance import PROBE_SECRET_ENV_VISIBILITY, load_verdicts

        monkeypatch.setenv("GH_TOKEN", self.MIMIC)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", self.MIMIC)
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)
        pair = provisioner.ensure_pair("r1")
        try:
            fallback = [e for e in events if e.type == "sandbox.secret_env_fallback"]
            assert fallback, "mimic sentinel must trigger the env-file fallback"
            cached = load_verdicts(tmp_path / "state", "0.38.0")
            assert cached[PROBE_SECRET_ENV_VISIBILITY].verdict == "sentinel-under-exec"
        finally:
            pair.cleanup()

    def test_shadow_probe_ignores_mimic(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """App mode on a template that stamps the mimic: the worker
        overrides sentinels, so this is not a shadow — provisioning must
        proceed."""
        TestGithubAppAuth().stub_mint(monkeypatch)
        monkeypatch.setenv("GH_TOKEN", self.MIMIC)
        provisioner = make_provisioner(fake_sbx, tmp_path, env=TestGithubAppAuth.APP_ENV)
        pair = provisioner.ensure_pair("r1")
        pair.cleanup()


class TestBotLoginResolution:
    """`Provisioner.gh_bot_login` (#569 x #536): the write-attribution
    identity, knowable from the credential alone only in App mode."""

    def test_pat_mode_answers_none(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        assert make_provisioner(fake_sbx, tmp_path).gh_bot_login(None) is None

    def test_app_mode_answers_the_bot_login(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.gh import appauth

        monkeypatch.setattr(appauth, "fetch_app_slug", lambda creds, **kw: "sbxloop-app")
        provisioner = make_provisioner(fake_sbx, tmp_path, env=TestGithubAppAuth.APP_ENV)
        assert provisioner.gh_bot_login(None) == "sbxloop-app[bot]"
        # cached on the shared source: a second ask fetches nothing new
        assert provisioner.gh_bot_login(None) == "sbxloop-app[bot]"

    def test_a_failed_slug_lookup_degrades_to_none(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.errors import GithubOpsError
        from sbxloop.gh import appauth

        def boom(creds: object, **kw: object) -> str:
            raise GithubOpsError("nope")

        monkeypatch.setattr(appauth, "fetch_app_slug", boom)
        provisioner = make_provisioner(fake_sbx, tmp_path, env=TestGithubAppAuth.APP_ENV)
        assert provisioner.gh_bot_login(None) is None

    def test_a_misconfigured_credential_answers_none_not_raise(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """PAT + App both set raises at provisioning time, not here."""
        provisioner = make_provisioner(
            fake_sbx, tmp_path, env={**TestGithubAppAuth.APP_ENV, "GH_TOKEN": "github_pat_x"}
        )
        assert provisioner.gh_bot_login(None) is None

    def test_a_per_repo_token_env_stays_a_pat(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        config = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repos": [{"repo": "owner/repo", "token_env": "GH_TOKEN_TWO"}]},
            }
        )
        provisioner = make_provisioner(
            fake_sbx,
            tmp_path,
            env={**TestGithubAppAuth.APP_ENV, "GH_TOKEN_TWO": "github_pat_two"},
            config=config,
        )
        assert provisioner.gh_bot_login("owner/repo") is None


class TestStdinEnvDelivery:
    """Per-job stdin secret delivery (#592): when the exec-stdin-env probe
    passes, non-proxy credential delivery skips the in-VM env file entirely
    and WorkerClient pipes exports per job via Provisioner.job_env."""

    def _provision(
        self,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        *,
        stdin_ok: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[Provisioner, list[Event]]:
        if stdin_ok:
            monkeypatch.setenv("SBX_FAKE_EXEC_STDIN", "1")
        bus = EventBus()
        events: list[Event] = []
        bus.subscribe(events.append)
        provisioner = make_provisioner(fake_sbx, tmp_path, bus=bus)
        # no cleanup: the assertions below inspect the fake sandbox fs, which
        # `sbx rm` would delete; tmp_path reaps everything anyway
        provisioner.ensure_pair("r1")
        return provisioner, events

    def _env_files(self, fake_sbx: FakeSbx) -> list[Path]:
        return [
            fake_sbx.sandbox_fs(name) / "home/agent/.sbxloop/env.sh"
            for name in ("sbxloop-r1-agent", "sbxloop-r1-github")
        ]

    def test_fallback_delivers_via_stdin_when_probe_passes(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proxy invisible + stdin passes through: the downgrade event still
        fires, but no token value is ever written into the VM."""
        _provisioner, events = self._provision(
            fake_sbx, tmp_path, stdin_ok=True, monkeypatch=monkeypatch
        )
        fallback = [e for e in events if e.type == "sandbox.secret_env_fallback"]
        assert sorted(e.data["env"] for e in fallback) == ["COPILOT_GITHUB_TOKEN", "GH_TOKEN"]
        assert all(e.data["delivery"] == "stdin" for e in fallback)
        for env_file in self._env_files(fake_sbx):
            if env_file.exists():
                content = env_file.read_text()
                assert all(token not in content for token in TOKENS.values())
        # the verdict is cached for this sbx version
        from sbxloop.sbx.conformance import PROBE_EXEC_STDIN_ENV, load_verdicts

        record = load_verdicts(tmp_path / "state", "0.38.0").get(PROBE_EXEC_STDIN_ENV)
        assert record is not None and record.verdict == "delivers"

    def test_fallback_writes_env_file_when_probe_fails(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No stdin passthrough (the fake's default): exactly today's
        env-file behavior, and job_env offers no provider."""
        provisioner, events = self._provision(
            fake_sbx, tmp_path, stdin_ok=False, monkeypatch=monkeypatch
        )
        fallback = [e for e in events if e.type == "sandbox.secret_env_fallback"]
        assert fallback and all(e.data["delivery"] == "env-file" for e in fallback)
        agent_env = self._env_files(fake_sbx)[0]
        assert agent_env.is_file()
        assert TOKENS["COPILOT_GITHUB_TOKEN"] in agent_env.read_text()
        assert provisioner.job_env("agent") is None
        assert provisioner.job_env("github", "owner/repo") is None

    def test_job_env_providers_after_stdin_provision(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provisioner, _ = self._provision(fake_sbx, tmp_path, stdin_ok=True, monkeypatch=monkeypatch)
        agent = provisioner.job_env("agent")
        assert agent is not None
        assert agent() == {"COPILOT_GITHUB_TOKEN": TOKENS["COPILOT_GITHUB_TOKEN"]}
        github = provisioner.job_env("github", "owner/repo")
        assert github is not None
        exports = github()
        assert exports["GH_TOKEN"] == TOKENS["GH_TOKEN"]
        assert exports["GITHUB_TOKEN"] == TOKENS["GH_TOKEN"]
        assert exports["GH_REPO"] == "owner/repo"

    def test_job_env_is_none_when_proxy_works(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A visible proxy secret keeps the token out of the VM entirely —
        stdin delivery must not activate on top of it."""
        monkeypatch.setenv("SBX_FAKE_EXEC_STDIN", "1")
        # the shadow/visibility probes classify what the shell sees; export
        # a token-shaped value so the proxy counts as working
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "github_pat_visible")
        monkeypatch.setenv("GH_TOKEN", "github_pat_visible")
        provisioner = make_provisioner(fake_sbx, tmp_path)
        provisioner.ensure_pair("r1")
        assert provisioner.job_env("agent") is None
        assert provisioner.job_env("github", "owner/repo") is None

    def test_gh_refresher_is_none_under_stdin_delivery(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """App mode + stdin delivery: nothing in the VM to refresh — the
        job_env provider re-mints per job through the token source."""
        from sbxloop.sbx.conformance import VERDICT_STDIN_DELIVERS
        from sbxloop.sbx.sandbox import Sandbox as _Sandbox

        provisioner = make_provisioner(fake_sbx, tmp_path, env=TestGithubAppAuth.APP_ENV)
        monkeypatch.setattr(provisioner, "_cached_verdict", lambda probe_id: VERDICT_STDIN_DELIVERS)
        sandbox = _Sandbox(provisioner.cli, "sbxloop-r1-github")
        assert provisioner.gh_refresher(sandbox, "owner/repo") is None


class TestClaudeAgentBackend:
    """[agent] backend = "claude" (#533): credential, spec, and env plumbing."""

    CLAUDE_ENV: ClassVar[dict[str, str]] = {
        "ANTHROPIC_API_KEY": "sk-ant-claude-agent-key",
        "GH_TOKEN": "github_pat_user",
    }

    def _provisioner(self, fake_sbx: FakeSbx, tmp_path: Path, **kwargs: object) -> Provisioner:
        config = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "agent": {"backend": "claude"}, **GITHUB_ENABLED}
        )
        kwargs.setdefault("env", self.CLAUDE_ENV)
        return make_provisioner(fake_sbx, tmp_path, config=config, **kwargs)  # type: ignore[arg-type]

    def test_agent_spec_carries_anthropic_credential_and_egress(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        provisioner = self._provisioner(fake_sbx, tmp_path)
        agent, _github = provisioner.build_specs("r1", tmp_path)
        assert [(s.host, s.env) for s in agent.secrets] == [
            ("api.anthropic.com", "ANTHROPIC_API_KEY")
        ]
        assert "api.anthropic.com" in agent.policy_allows
        assert agent.persistent_env["SBXLOOP_WORKER_BACKEND"] == "claude"
        assert agent.persistent_env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"

    def test_copilot_default_is_unchanged(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = make_provisioner(fake_sbx, tmp_path)
        agent, _github = provisioner.build_specs("r1", tmp_path)
        assert [(s.host, s.env) for s in agent.secrets] == [
            ("api.github.com", "COPILOT_GITHUB_TOKEN")
        ]
        assert "api.anthropic.com" not in agent.policy_allows
        assert "SBXLOOP_WORKER_BACKEND" not in agent.persistent_env

    def test_missing_anthropic_key_fails_fast(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        provisioner = self._provisioner(fake_sbx, tmp_path, env={"GH_TOKEN": "github_pat_user"})
        with pytest.raises(ProvisionError, match="ANTHROPIC_API_KEY"):
            provisioner.agent_token()

    def test_invalid_backend_fails_config_loading(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="backend"):
            Config.model_validate({"state_dir": str(tmp_path), "agent": {"backend": "gemini"}})

    def test_job_env_delivers_anthropic_key_and_backend_selector(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under stdin delivery the agent provider carries the API key AND
        the worker's backend selector, so a box with no env file still runs
        the claude backend."""
        monkeypatch.setenv("SBX_FAKE_EXEC_STDIN", "1")
        provisioner = self._provisioner(fake_sbx, tmp_path)
        provisioner.ensure_pair("r1")
        provider = provisioner.job_env("agent")
        assert provider is not None
        exports = provider()
        assert exports["ANTHROPIC_API_KEY"] == "sk-ant-claude-agent-key"
        assert exports["SBXLOOP_WORKER_BACKEND"] == "claude"

    def test_env_file_fallback_writes_anthropic_key(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        provisioner = self._provisioner(fake_sbx, tmp_path)
        provisioner.ensure_pair("r1")
        env_file = fake_sbx.sandbox_fs("sbxloop-r1-agent") / "home/agent/.sbxloop/env.sh"
        content = env_file.read_text()
        assert "ANTHROPIC_API_KEY=sk-ant-claude-agent-key" in content
        assert "SBXLOOP_WORKER_BACKEND=claude" in content
        assert "COPILOT_GITHUB_TOKEN" not in content
