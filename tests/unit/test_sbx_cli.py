"""SbxCLI wrapper tests against the fake sbx harness."""

import subprocess
from pathlib import Path

import pytest

from sbxloop.errors import SbxError, SbxNotFoundError
from sbxloop.sbx.cli import SbxCLI, _exec_failed_at_sbx_level, redacted_argv
from sbxloop.sbx.models import SandboxSpec, SecretSpec
from tests.conftest import FakeSbx


@pytest.fixture
def cli(fake_sbx: FakeSbx) -> SbxCLI:
    return SbxCLI(binary=str(fake_sbx.binary))


def spec(name: str = "sbxloop-r1-agent", tmp: Path = Path("/tmp")) -> SandboxSpec:
    return SandboxSpec(name=name, role="agent", workspace=tmp)


class TestAppName:
    def test_no_app_name_by_default(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        # Default shares the user's normal sbx state so their login and
        # policy init apply; isolation is opt-in.
        cli.run("version", check=False)
        assert fake_sbx.raw_invocations()[0]["args"] == ["version"]
        assert cli.argv("ls") == [str(fake_sbx.binary), "ls"]

    def test_app_name_opt_in_injected(self, fake_sbx: FakeSbx) -> None:
        cli = SbxCLI(binary=str(fake_sbx.binary), app_name="sbxloop")
        assert cli.argv("version")[:3] == [str(fake_sbx.binary), "--app-name", "sbxloop"]
        cli.run("version", check=False)
        # the fake strips --app-name before recording, proving it was passed
        assert fake_sbx.raw_invocations()[0]["args"] == ["version"]


class TestLifecycle:
    def test_create_and_ls(self, cli: SbxCLI, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        cli.create(spec("boxa", tmp_path))
        cli.create(
            SandboxSpec(
                name="boxb",
                role="github",
                workspace=tmp_path,
                template="docker/sandbox-templates:shell",
            )
        )
        infos = cli.ls()
        assert [i.name for i in infos] == ["boxa", "boxb"]
        assert fake_sbx.meta("boxb")["template"] == "docker/sandbox-templates:shell"
        assert fake_sbx.meta("boxa")["status"] == "running"

    def test_stop_and_rm(self, cli: SbxCLI, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        cli.create(spec("boxa", tmp_path))
        cli.stop("boxa")
        assert fake_sbx.meta("boxa")["status"] == "stopped"
        cli.rm("boxa")
        assert cli.ls() == []
        assert ["rm", "--force", "boxa"] in fake_sbx.invocations("rm")

    def test_rm_missing_raises_not_found(self, cli: SbxCLI) -> None:
        with pytest.raises(SbxNotFoundError):
            cli.rm("ghost")

    def test_version(self, cli: SbxCLI) -> None:
        assert cli.version() == "0.38.0"


class TestExec:
    def test_exec_runs_and_returns_inner_exit_code(self, cli: SbxCLI, tmp_path: Path) -> None:
        cli.create(spec("boxa", tmp_path))
        ok = cli.exec("boxa", ["sh", "-c", "echo hello"])
        assert ok.ok
        fail = cli.exec("boxa", ["sh", "-c", "exit 7"])
        assert fail.returncode == 7  # inner code propagates, no raise

    def test_exec_missing_sandbox_raises(self, cli: SbxCLI) -> None:
        with pytest.raises(SbxNotFoundError):
            cli.exec("ghost", ["true"])

    def test_exec_stopped_sandbox_raises_infra_error(self, cli: SbxCLI, tmp_path: Path) -> None:
        # "is not running" is an sbx-level refusal, not the inner command's
        # exit code — it must raise instead of masquerading as a result (#63)
        cli.create(spec("boxa", tmp_path))
        cli.stop("boxa")
        with pytest.raises(SbxError):
            cli.exec("boxa", ["true"])

    def test_exec_daemon_failure_raises(
        self, cli: SbxCLI, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        cli.create(spec("boxa", tmp_path))
        fake_sbx.fail_next(
            "exec boxa",
            returncode=1,
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        )
        with pytest.raises(SbxError) as excinfo:
            cli.exec("boxa", ["true"])
        assert not isinstance(excinfo.value, SbxNotFoundError)

    def test_exec_inner_failure_with_stderr_still_returns(
        self, cli: SbxCLI, tmp_path: Path
    ) -> None:
        # Ordinary inner-command stderr must not trip the infra classifier.
        cli.create(spec("boxa", tmp_path))
        fail = cli.exec("boxa", ["sh", "-c", "echo 'connection refused by host' >&2; exit 7"])
        assert fail.returncode == 7
        assert "connection refused" in fail.stderr

    def test_exec_never_inherits_the_callers_stdin(
        self, cli: SbxCLI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `sbx exec` attaches inherited stdin (that IS exec_interactive's
        # mechanism), so background execs launched during a TUI run would
        # otherwise steal the chat form's keystrokes.
        captured: dict[str, object] = {}
        real_run = subprocess.run

        def spy(argv: list[str], **kwargs: object) -> object:
            captured.update(kwargs)
            return real_run(argv, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("sbxloop.sbx.cli.subprocess.run", spy)
        cli.run("version", check=False)
        assert captured["stdin"] is subprocess.DEVNULL

        captured.clear()
        cli.run("secret", "set", "github", stdin="tok", check=False)
        # An explicit stdin payload still flows through `input=` (which pipes).
        assert captured["input"] == "tok"
        assert captured["stdin"] is None

    def test_popen_never_inherits_the_callers_stdin(
        self, cli: SbxCLI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli.create(spec("boxa", tmp_path))
        captured: dict[str, object] = {}
        real_popen = subprocess.Popen

        class Spy(real_popen):  # type: ignore[valid-type, misc]
            def __init__(self, argv: list[str], **kwargs: object) -> None:
                captured.update(kwargs)
                super().__init__(argv, **kwargs)

        monkeypatch.setattr("sbxloop.sbx.cli.subprocess.Popen", Spy)
        proc = cli.popen("exec", "boxa", "true")
        try:
            assert captured["stdin"] is subprocess.DEVNULL
        finally:
            proc.communicate(timeout=30)

    def test_exec_writes_inside_fs(self, cli: SbxCLI, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        cli.create(spec("boxa", tmp_path))
        cli.exec(
            "boxa",
            ["sh", "-c", "mkdir -p /home/agent/.sbxloop && echo hi > /home/agent/.sbxloop/x"],
        )
        assert (fake_sbx.sandbox_fs("boxa") / "home/agent/.sbxloop/x").read_text() == "hi\n"


class TestCp:
    def test_cp_roundtrip(self, cli: SbxCLI, tmp_path: Path) -> None:
        cli.create(spec("boxa", tmp_path))
        local = tmp_path / "payload.json"
        local.write_text('{"v": 1}')
        cli.cp(str(local), "boxa:/home/agent/payload.json")
        out = tmp_path / "back.json"
        cli.cp("boxa:/home/agent/payload.json", str(out))
        assert out.read_text() == '{"v": 1}'

    def test_cp_missing_source_raises(self, cli: SbxCLI, tmp_path: Path) -> None:
        cli.create(spec("boxa", tmp_path))
        with pytest.raises(SbxNotFoundError):
            cli.cp(str(tmp_path / "nope"), "boxa:/x")


class TestErrors:
    def test_binary_missing(self, tmp_path: Path) -> None:
        cli = SbxCLI(binary=str(tmp_path / "no-such-sbx"))
        with pytest.raises(SbxNotFoundError, match="not found on PATH"):
            cli.run("ls")

    def test_scripted_failure_maps_to_sbx_error(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        fake_sbx.fail_next("ls", returncode=125, stderr="daemon exploded")
        with pytest.raises(SbxError) as excinfo:
            cli.ls()
        assert excinfo.value.returncode == 125
        assert "daemon exploded" in excinfo.value.stderr
        assert not isinstance(excinfo.value, SbxNotFoundError)

    def test_fail_next_is_once(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        fake_sbx.fail_next("version", returncode=1, stderr="flake")
        assert cli.run("version", check=False).returncode == 1
        assert cli.version() == "0.38.0"

    def test_check_false_returns_result(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        fake_sbx.fail_next("ls", returncode=3, stderr="x")
        result = cli.run("ls", check=False)
        assert result.returncode == 3


class TestPolicy:
    def test_policy_allow_global_and_scoped(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        cli.policy_allow("api.githubcopilot.com")
        cli.policy_allow(domain="api.github.com", sandbox="boxa")
        assert fake_sbx.policies() == [
            ["allow", "network", "api.githubcopilot.com"],
            ["allow", "network", "api.github.com", "--sandbox", "boxa"],
        ]

    def test_policy_allow_batches_domains_into_one_invocation(
        self, cli: SbxCLI, fake_sbx: FakeSbx
    ) -> None:
        cli.policy_allow("api.github.com", "*.githubcopilot.com", "pypi.org", sandbox="boxa")
        assert fake_sbx.invocations("policy allow network") == [
            [
                "policy",
                "allow",
                "network",
                "api.github.com,*.githubcopilot.com,pypi.org",
                "--sandbox",
                "boxa",
            ]
        ]
        # the recorder expands the comma-list back into per-domain rules
        assert ["allow", "network", "pypi.org", "--sandbox", "boxa"] in fake_sbx.policies()

    def test_policy_allow_without_domains_is_a_no_op(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        cli.policy_allow(sandbox="boxa")
        assert fake_sbx.policies() == []

    def test_policy_check_allowed(self, cli: SbxCLI) -> None:
        assert cli.policy_check("api.github.com") is True

    def test_policy_check_denied_output(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        fake_sbx.script("policy check network evil.example", stdout="denied by policy\n")
        assert cli.policy_check("evil.example") is False

    def test_policy_check_denied_via_nonzero_exit_is_false(
        self, cli: SbxCLI, fake_sbx: FakeSbx
    ) -> None:
        # sbx answering "denied" with a nonzero exit is still a policy answer
        fake_sbx.script("policy check", returncode=1, stdout="denied by policy\n", once=True)
        assert cli.policy_check("x.example") is False

    def test_policy_check_invocation_failure_raises(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        # a failed invocation with no deny-shaped answer is infra trouble,
        # not "blocked" (#63)
        fake_sbx.fail_next("policy check", returncode=1, stderr="no policy")
        with pytest.raises(SbxError):
            cli.policy_check("x.example")

    def test_policy_init(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        cli.policy_init("balanced")
        assert ["init", "balanced"] in fake_sbx.policies()


class TestSecrets:
    def test_secret_set_global_service_with_stdin_token(
        self, cli: SbxCLI, fake_sbx: FakeSbx
    ) -> None:
        cli.secret_set("github", token="ghp_test123")
        records = fake_sbx.secrets()
        assert records[0]["args"] == ["set", "-g", "github"]
        assert records[0]["stdin"] == "ghp_test123"

    def test_secret_set_sandbox_scoped(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        cli.secret_set("github", sandbox="boxa")
        assert fake_sbx.secrets()[0]["args"] == ["set", "boxa", "github"]

    def test_secret_set_custom(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        cli.secret_set_custom(
            host="api.githubcopilot.com",
            env="COPILOT_GITHUB_TOKEN",
            value="github_pat_x",
            sandbox="boxa",
        )
        assert fake_sbx.secrets()[0]["args"] == [
            "set-custom",
            "boxa",
            "--host",
            "api.githubcopilot.com",
            "--env",
            "COPILOT_GITHUB_TOKEN",
            "--value",
            "github_pat_x",
        ]


class TestSecretRedaction:
    """Secret values must never surface through argv-carrying errors/results.

    The subprocess still receives the real value (asserted by
    test_secret_set_custom above); everything observable — ExecResult.argv,
    SbxError.argv, and therefore str(exc), logs, and events — must carry the
    masked copy.
    """

    def test_redacted_argv_masks_flag_value(self) -> None:
        argv = ["sbx", "secret", "set-custom", "-g", "--value", "github_pat_SECRET"]
        assert redacted_argv(argv) == ["sbx", "secret", "set-custom", "-g", "--value", "***"]

    def test_redacted_argv_masks_equals_form(self) -> None:
        argv = ["sbx", "secret", "set-custom", "--value=github_pat_SECRET"]
        assert redacted_argv(argv) == ["sbx", "secret", "set-custom", "--value=***"]

    def test_redacted_argv_leaves_normal_args_alone(self) -> None:
        argv = ["sbx", "exec", "boxa", "echo", "hi"]
        assert redacted_argv(argv) == argv

    def test_trailing_secret_flag_without_value(self) -> None:
        argv = ["sbx", "secret", "set-custom", "--value"]
        assert redacted_argv(argv) == argv

    def test_set_custom_failure_never_leaks_the_value(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        fake_sbx.fail_next("secret set-custom", returncode=125, stderr="daemon exploded")
        with pytest.raises(SbxError) as excinfo:
            cli.secret_set_custom(
                host="api.github.com",
                env="COPILOT_GITHUB_TOKEN",
                value="github_pat_SUPERSECRET",
                sandbox="boxa",
            )
        text = str(excinfo.value)
        assert "github_pat_SUPERSECRET" not in text
        assert "***" in text
        assert "github_pat_SUPERSECRET" not in " ".join(excinfo.value.argv)

    def test_exec_interactive_missing_binary_redacts_argv(self, tmp_path: Path) -> None:
        cli = SbxCLI(binary=str(tmp_path / "no-such-sbx"))
        with pytest.raises(SbxNotFoundError) as excinfo:
            cli.exec_interactive("boxa", ["some-tool", "--value", "tok_SECRET"])
        assert "tok_SECRET" not in " ".join(excinfo.value.argv)
        assert "***" in excinfo.value.argv

    def test_exec_result_argv_is_redacted(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        result = cli.run(
            "secret", "set-custom", "-g", "--host", "h", "--env", "E", "--value", "tok_SECRET"
        )
        assert "tok_SECRET" not in result.argv
        assert "***" in result.argv


class TestSecretSpecModel:
    def test_service_spec(self) -> None:
        s = SecretSpec(kind="service", service="github")
        assert s.service == "github"

    def test_custom_spec_requires_host_env(self) -> None:
        with pytest.raises(ValueError, match="requires host and env"):
            SecretSpec(kind="custom", host="api.example.com")

    def test_service_spec_rejects_host(self) -> None:
        with pytest.raises(ValueError, match="must not set host"):
            SecretSpec(kind="service", service="github", host="x")


class TestExecFailureClassification:
    """`sbx exec` stderr belongs to the INNER command, so ordinary shell
    failures must come back as a nonzero result, not a raised SbxError."""

    @pytest.mark.parametrize(
        "stderr",
        [
            "sh: 1: dpkg: not found",
            "bash: cargo: command not found",
            "curl: (22) The requested URL returned error: 404 Not Found",
            "npm ERR! 404 Not Found - GET https://registry.npmjs.org/nope",
        ],
    )
    def test_inner_command_not_found_is_not_infra(self, stderr: str) -> None:
        assert not _exec_failed_at_sbx_level(stderr)

    @pytest.mark.parametrize(
        "stderr",
        [
            'Error: sandbox "sbxloop-r1-agent" not found',
            "Error: no such sandbox: sbxloop-r1-agent",
            "Error: sandbox is not running",
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        ],
    )
    def test_sbx_level_failures_still_raise(self, stderr: str) -> None:
        assert _exec_failed_at_sbx_level(stderr)


class TestInvocationLogging:
    """Every sbx call is a DEBUG line (verb, redacted argv, rc, duration);
    a failing one carries its stderr; secrets never reach the log."""

    def test_invoke_logged_with_redacted_argv(
        self, cli: SbxCLI, fake_sbx: FakeSbx, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        with caplog.at_level(logging.DEBUG, logger="sbxloop.sbx.cli"):
            cli.secret_set_custom(host="h", env="E", value="github_pat_SECRET")
        lines = [r.getMessage() for r in caplog.records if "sbx.invoke" in r.getMessage()]
        assert lines, [r.getMessage() for r in caplog.records]
        assert all("github_pat_SECRET" not in line for line in lines)
        assert any("'command': 'secret set-custom'" in line for line in lines)
        assert any("'rc': 0" in line and "'duration_s'" in line for line in lines)

    def test_failed_invoke_carries_stderr(
        self, cli: SbxCLI, fake_sbx: FakeSbx, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        fake_sbx.fail_next("rm", returncode=1, stderr="no such sandbox")
        with caplog.at_level(logging.DEBUG, logger="sbxloop.sbx.cli"):
            with pytest.raises(SbxError):
                cli.rm("nope")
        (line,) = [r.getMessage() for r in caplog.records if "sbx.invoke" in r.getMessage()]
        assert "'command': 'rm'" in line and "'rc': 1" in line
        assert "no such sandbox" in line
