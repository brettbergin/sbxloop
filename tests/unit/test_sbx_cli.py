"""SbxCLI wrapper tests against the fake sbx harness."""

from pathlib import Path

import pytest

from sdxloop.errors import SbxError, SbxNotFoundError
from sdxloop.sbx.cli import SbxCLI
from sdxloop.sbx.models import SandboxSpec, SecretSpec
from tests.conftest import FakeSbx


@pytest.fixture
def cli(fake_sbx: FakeSbx) -> SbxCLI:
    return SbxCLI(binary=str(fake_sbx.binary))


def spec(name: str = "sdxloop-r1-agent", tmp: Path = Path("/tmp")) -> SandboxSpec:
    return SandboxSpec(name=name, role="agent", workspace=tmp)


class TestAppName:
    def test_app_name_injected(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        cli.run("version", check=False)
        raw = fake_sbx.raw_invocations()
        assert raw[0]["args"] == ["version"]  # app-name stripped by fake, so recorded clean
        assert cli.argv("version")[:3] == [str(fake_sbx.binary), "--app-name", "sdxloop"]

    def test_app_name_disabled(self, fake_sbx: FakeSbx) -> None:
        cli = SbxCLI(binary=str(fake_sbx.binary), app_name=None)
        assert cli.argv("ls") == [str(fake_sbx.binary), "ls"]


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
        assert cli.version() == "0.35.0"


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

    def test_exec_writes_inside_fs(self, cli: SbxCLI, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        cli.create(spec("boxa", tmp_path))
        cli.exec(
            "boxa",
            ["sh", "-c", "mkdir -p /home/agent/.sdxloop && echo hi > /home/agent/.sdxloop/x"],
        )
        assert (fake_sbx.sandbox_fs("boxa") / "home/agent/.sdxloop/x").read_text() == "hi\n"


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
        assert cli.version() == "0.35.0"

    def test_check_false_returns_result(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        fake_sbx.fail_next("ls", returncode=3, stderr="x")
        result = cli.run("ls", check=False)
        assert result.returncode == 3


class TestPolicy:
    def test_policy_allow_global_and_scoped(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        cli.policy_allow("api.githubcopilot.com")
        cli.policy_allow("api.github.com", sandbox="boxa")
        assert fake_sbx.policies() == [
            ["allow", "network", "api.githubcopilot.com"],
            ["allow", "network", "api.github.com", "--sandbox", "boxa"],
        ]

    def test_policy_check_allowed(self, cli: SbxCLI) -> None:
        assert cli.policy_check("api.github.com") is True

    def test_policy_check_denied_output(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        fake_sbx.script("policy check network evil.example", stdout="denied by policy\n")
        assert cli.policy_check("evil.example") is False

    def test_policy_check_nonzero_is_false(self, cli: SbxCLI, fake_sbx: FakeSbx) -> None:
        fake_sbx.fail_next("policy check", returncode=1, stderr="no policy")
        assert cli.policy_check("x.example") is False

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
