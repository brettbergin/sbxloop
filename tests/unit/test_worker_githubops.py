"""Worker github.op tests: HTTP status as a structured field (#221) and
probes answering "missing" as data instead of raising (#222).

Field failure rgwp5z40x: the host matched "HTTP 404" in the error prose while
real GitHub (via gh) said "(HTTP 409)". These tests pin that both transports
put the status on ``GithubOpError.http_status``, that the runner carries it
onto ``ErrorInfo``, and that ``repo.get`` / ``ref.get`` under
``allow_missing`` turn the expected "no" into an ok result. The op registry
runs against a scripted Transport (no gh, no network).
"""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from pathlib import Path
from typing import Any, ClassVar

import pytest

from sbxloop_worker import githubops
from sbxloop_worker.githubops import (
    GhCliTransport,
    GithubOpError,
    JsonValue,
    RestTransport,
    execute_op,
    parse_gh_http_status,
)
from sbxloop_worker.protocol import JobRequest
from sbxloop_worker.runner import JobRunner


class TestParseGhHttpStatus:
    @pytest.mark.parametrize(
        ("stderr", "expected"),
        [
            ("gh: Git Repository is empty. (HTTP 409)", 409),
            ("gh: Not Found (HTTP 404)", 404),
            ("HTTP 502: Bad Gateway", 502),
            # gh's parenthesized status is authoritative over a status quoted
            # inside the server message; the last parenthesized one wins.
            ("gh: upstream said HTTP 404 (HTTP 500)", 500),
            ("gh: retried after (HTTP 502) then failed (HTTP 409)", 409),
            ("gh: could not connect", None),
            ("gh: 12345 items", None),
        ],
    )
    def test_cases(self, stderr: str, expected: int | None) -> None:
        assert parse_gh_http_status(stderr) == expected


class ScriptedTransport:
    """Answers each request from a path -> (json | GithubOpError) table."""

    def __init__(self, table: dict[str, Any]) -> None:
        self.table = table
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> JsonValue:
        self.calls.append((method, path))
        answer = self.table[path]
        if isinstance(answer, Exception):
            raise answer
        result: JsonValue = answer
        return result


def http_error(status: int, message: str = "nope") -> GithubOpError:
    return GithubOpError(f"gh: {message} (HTTP {status})", http_status=status)


class TestRepoGet:
    def test_missing_is_data_only_when_asked(self) -> None:
        t = ScriptedTransport({"/repos/o/r": http_error(404, "Not Found")})
        # Default: a 404 is still an error (callers that did not opt in must
        # not silently get an empty dict).
        with pytest.raises(GithubOpError, match="404"):
            execute_op("repo.get", {"repo": "o/r"}, transport=t)
        assert execute_op("repo.get", {"repo": "o/r", "allow_missing": True}, transport=t) == {
            "missing": True,
            "http_status": 404,
        }

    def test_other_statuses_still_raise_with_allow_missing(self) -> None:
        t = ScriptedTransport({"/repos/o/r": http_error(403, "rate limited")})
        with pytest.raises(GithubOpError, match="403"):
            execute_op("repo.get", {"repo": "o/r", "allow_missing": True}, transport=t)

    def test_present_repo_passes_through(self) -> None:
        t = ScriptedTransport({"/repos/o/r": {"full_name": "o/r"}})
        assert execute_op("repo.get", {"repo": "o/r", "allow_missing": True}, transport=t) == {
            "full_name": "o/r"
        }


class TestRefGet:
    def test_resolves_sha(self) -> None:
        t = ScriptedTransport(
            {"/repos/o/r/git/ref/heads/main": {"ref": "refs/heads/main", "object": {"sha": "abc"}}}
        )
        assert execute_op("ref.get", {"repo": "o/r", "ref": "heads/main"}, transport=t) == {
            "ref": "refs/heads/main",
            "sha": "abc",
        }

    @pytest.mark.parametrize("status", [404, 409])
    def test_missing_and_empty_repo_are_data(self, status: int) -> None:
        """404 (absent branch) and 409 (empty repository — the shape real
        GitHub gave run rgwp5z40x) both mean 'no base to build on'."""
        t = ScriptedTransport({"/repos/o/r/git/ref/heads/main": http_error(status)})
        params = {"repo": "o/r", "ref": "heads/main", "allow_missing": True}
        assert execute_op("ref.get", params, transport=t) == {
            "missing": True,
            "http_status": status,
        }
        with pytest.raises(GithubOpError, match=str(status)):
            execute_op("ref.get", {"repo": "o/r", "ref": "heads/main"}, transport=t)

    def test_unrelated_status_raises(self) -> None:
        t = ScriptedTransport({"/repos/o/r/git/ref/heads/main": http_error(403)})
        with pytest.raises(GithubOpError, match="403"):
            execute_op(
                "ref.get", {"repo": "o/r", "ref": "heads/main", "allow_missing": True}, transport=t
            )

    def test_malformed_response_raises(self) -> None:
        t = ScriptedTransport({"/repos/o/r/git/ref/heads/main": {"message": "weird"}})
        with pytest.raises(GithubOpError, match="no object sha"):
            execute_op("ref.get", {"repo": "o/r", "ref": "heads/main"}, transport=t)


class TestLabelGet:
    """#556: the follow-up label probe. An absent label is the routine
    answer to an existence question, so with ``allow_missing`` a 404 comes
    back as data; a 403/5xx is a real failure and still raises."""

    PATH = "/repos/o/r/labels/sbxloop%3Afollow-up"
    PARAMS: ClassVar[dict[str, Any]] = {"repo": "o/r", "name": "sbxloop:follow-up"}

    def test_present_label_passes_through(self) -> None:
        t = ScriptedTransport({self.PATH: {"name": "sbxloop:follow-up", "color": "c5def5"}})
        assert execute_op("label.get", {**self.PARAMS, "allow_missing": True}, transport=t) == {
            "name": "sbxloop:follow-up",
            "color": "c5def5",
        }
        assert t.calls == [("GET", self.PATH)]

    def test_missing_is_data_only_when_asked(self) -> None:
        t = ScriptedTransport({self.PATH: http_error(404, "Not Found")})
        assert execute_op("label.get", {**self.PARAMS, "allow_missing": True}, transport=t) == {
            "missing": True,
            "http_status": 404,
        }
        with pytest.raises(GithubOpError, match="404"):
            execute_op("label.get", dict(self.PARAMS), transport=t)

    @pytest.mark.parametrize("status", [403, 500, 502])
    def test_other_statuses_still_raise_with_allow_missing(self, status: int) -> None:
        """A token without repo scope, or GitHub being unwell, is not
        'the label is absent' — the caller must not fall through to a POST."""
        t = ScriptedTransport({self.PATH: http_error(status)})
        with pytest.raises(GithubOpError, match=str(status)):
            execute_op("label.get", {**self.PARAMS, "allow_missing": True}, transport=t)

    def test_malformed_response_raises(self) -> None:
        t = ScriptedTransport({self.PATH: ["not", "a", "label"]})
        with pytest.raises(GithubOpError, match="no label object"):
            execute_op("label.get", dict(self.PARAMS), transport=t)


class TestLabelProbeEmitsNoErrorEvent:
    """The regression the #518 ref lookup fixed, for labels: a repository
    that does not yet carry the follow-up label must not pay a worker
    ``error`` event (a red panel in the run chronology) for asking."""

    @staticmethod
    def _run(tmp_path: Path, op: str, params: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
        events_path = tmp_path / f"{op}-events.jsonl"
        result_path = tmp_path / f"{op}-result.json"

        class Missing:
            def request(self, method: str, path: str, body: Any = None) -> Any:
                raise GithubOpError(
                    f"gh api GET {path} failed (rc=1): Not Found (HTTP 404)", http_status=404
                )

        job = JobRequest(job_id="j1", run_id="r1", kind="github.op", op=op, params=params)
        runner = JobRunner(job, events_path, result_path, heartbeat_s=0)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(githubops, "select_transport", Missing)
            result = runner.run()
        events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
        return result, events

    def test_absent_label_comes_back_as_data(self, tmp_path: Path) -> None:
        result, events = self._run(
            tmp_path,
            "label.get",
            {"repo": "o/r", "name": "sbxloop:follow-up", "allow_missing": True},
        )
        assert result.status == "ok"
        assert result.output_json == {"missing": True, "http_status": 404}
        assert [e for e in events if e.get("type") == "worker.error"] == []

    def test_without_allow_missing_the_miss_is_still_an_error(self, tmp_path: Path) -> None:
        """The old shape, pinned: a bare GET for an absent label is exactly
        the ``worker.error`` panel this change removes."""
        result, events = self._run(
            tmp_path, "label.get", {"repo": "o/r", "name": "sbxloop:follow-up"}
        )
        assert result.status == "error"
        assert result.error is not None and result.error.type == "GithubOpError"
        assert [e for e in events if e.get("type") == "worker.error"]


class TestGhCliTransport:
    def test_http_failure_carries_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="gh: Git Repository is empty. (HTTP 409)\n"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(GithubOpError) as info:
            GhCliTransport().request("GET", "/repos/o/r/git/ref/heads/main")
        assert info.value.http_status == 409
        assert "HTTP 409" in str(info.value)

    def test_non_http_failure_has_no_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 4, stdout="", stderr="gh: not logged in")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(GithubOpError) as info:
            GhCliTransport().request("GET", "/user")
        assert info.value.http_status is None


class TestRestTransport:
    def test_api_root_comes_from_the_hosts_export(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, str] = {}

        def fake_urlopen(request: Any, timeout: float = 0) -> Any:
            seen["url"] = request.full_url
            return io.BytesIO(b"{}")

        monkeypatch.setattr(githubops.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.delenv(githubops.API_URL_ENV, raising=False)
        RestTransport(token="t").request("GET", "/repos/o/r")
        assert seen["url"] == "https://api.github.com/repos/o/r"
        monkeypatch.setenv(githubops.API_URL_ENV, "https://ghe.example.com/api/v3/")
        RestTransport(token="t").request("GET", "/repos/o/r")
        assert seen["url"] == "https://ghe.example.com/api/v3/repos/o/r"

    def test_http_error_carries_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_urlopen(request: Any, timeout: float = 0) -> Any:
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", {}, io.BytesIO(b'{"message":"Not Found"}')
            )

        monkeypatch.setattr(githubops.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(GithubOpError) as info:
            RestTransport(token="t").request("GET", "/repos/o/r")
        assert info.value.http_status == 404

    def test_url_error_has_no_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_urlopen(request: Any, timeout: float = 0) -> Any:
            raise urllib.error.URLError("dns down")

        monkeypatch.setattr(githubops.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(GithubOpError) as info:
            RestTransport(token="t").request("GET", "/repos/o/r")
        assert info.value.http_status is None


class TestBlobsCreateMany:
    def test_wrapped_error_keeps_status(self) -> None:
        class Failing:
            def request(self, method: str, path: str, body: Any = None) -> Any:
                raise GithubOpError("POST blobs -> HTTP 403: nope", http_status=403)

        with pytest.raises(GithubOpError) as info:
            execute_op(
                "blobs.create_many",
                {"repo": "o/r", "files": [{"path": "a.txt", "content_b64": "YQ=="}]},
                transport=Failing(),
            )
        assert info.value.http_status == 403
        assert "'a.txt'" in str(info.value)


class TestRunnerErrorInfo:
    def _run(self, tmp_path: Path, exc: BaseException) -> Any:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise exc

        job = JobRequest(job_id="j1", run_id="r1", kind="github.op", op="raw.api", params={})
        runner = JobRunner(job, tmp_path / "events.jsonl", tmp_path / "result.json", heartbeat_s=0)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(githubops, "execute_op", boom)
            return runner.run()

    def test_github_op_error_status_lands_on_error_info(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, GithubOpError("gh: empty (HTTP 409)", http_status=409))
        assert result.status == "error"
        assert result.error is not None
        assert result.error.type == "GithubOpError"
        assert result.error.http_status == 409

    def test_other_exceptions_leave_status_unset(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, RuntimeError("boom"))
        assert result.error is not None
        assert result.error.http_status is None


class TestGithubOpsSandboxScoping:
    """Host-side provisioning of the github-ops sandbox is scoped to the
    run's repository: its token comes from that repo's ``token_env`` when it
    has one, its remote configuration names that repo — and the agent
    sandbox still receives no GitHub credential at all."""

    @staticmethod
    def _provisioner(tmp_path: Path, repos: list[dict[str, Any]], env: dict[str, str]):
        from sbxloop.config import Config
        from sbxloop.sbx.cli import SbxCLI
        from sbxloop.sbx.provision import Provisioner

        config = Config.model_validate(
            {"home": str(tmp_path / "state"), "github": {"repos": repos}}
        )
        return Provisioner(SbxCLI(binary="/bin/true"), config, env=env)

    def test_per_repo_token_env_wins(self, tmp_path: Path) -> None:
        provisioner = self._provisioner(
            tmp_path,
            [
                {"repo": "o/a", "token_env": "TOKEN_A"},
                {"repo": "o/b", "token_env": "TOKEN_B"},
            ],
            {"GH_TOKEN": "global", "TOKEN_A": "tok-a", "TOKEN_B": "tok-b"},
        )
        assert provisioner.gh_token("o/a") == "tok-a"
        assert provisioner.gh_token("o/b") == "tok-b"

    def test_falls_back_to_the_global_token(self, tmp_path: Path) -> None:
        provisioner = self._provisioner(
            tmp_path,
            [{"repo": "o/a", "token_env": "TOKEN_A"}, {"repo": "o/b"}],
            {"GH_TOKEN": "global", "TOKEN_A": "tok-a"},
        )
        assert provisioner.gh_token("o/b") == "global"

    def test_missing_per_repo_token_names_the_variable(self, tmp_path: Path) -> None:
        from sbxloop.errors import ProvisionError

        provisioner = self._provisioner(
            tmp_path, [{"repo": "o/a", "token_env": "TOKEN_A"}], {"GH_TOKEN": "global"}
        )
        with pytest.raises(ProvisionError, match="TOKEN_A"):
            provisioner.gh_token("o/a")

    def test_specs_carry_the_runs_repository(self, tmp_path: Path) -> None:
        provisioner = self._provisioner(
            tmp_path,
            [{"repo": "o/a"}, {"repo": "o/b"}],
            {"COPILOT_GITHUB_TOKEN": "copilot", "GH_TOKEN": "global"},
        )
        agent, github = provisioner.build_specs("r1", tmp_path, "o/b")
        assert github.persistent_env["SBXLOOP_GITHUB_REPO"] == "o/b"
        assert github.persistent_env["GH_REPO"] == "o/b"
        # The isolation invariant: the agent sandbox carries no GitHub
        # credential and no GitHub remote configuration — only the Copilot
        # token, bound to the token-exchange host.
        assert agent.persistent_env == {}
        assert [(s.host, s.env) for s in agent.secrets] == [
            ("api.github.com", "COPILOT_GITHUB_TOKEN")
        ]
        assert all(s.service != "github" for s in agent.secrets)
        assert [s.service for s in github.secrets] == ["github"]

    def test_daemon_wide_box_is_unscoped_when_several_repos(self, tmp_path: Path) -> None:
        """One box polling several repositories must not silently inherit the
        first repo's identity."""
        provisioner = self._provisioner(
            tmp_path, [{"repo": "o/a"}, {"repo": "o/b"}], {"GH_TOKEN": "global"}
        )
        assert provisioner.github_repo_env(None) == {}
        assert provisioner.gh_token(None) == "global"

    def test_enterprise_host_is_exported_to_the_github_sandbox(self, tmp_path: Path) -> None:
        """Both worker transports read what the host derived from
        [github] api_url (#623): GH_HOST for gh, the API URL for stdlib."""
        from sbxloop.config import Config
        from sbxloop.sbx.cli import SbxCLI
        from sbxloop.sbx.provision import Provisioner

        config = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repos": [{"repo": "o/a"}], "api_url": "https://ghe.example.com/api/v3"},
            }
        )
        provisioner = Provisioner(SbxCLI(binary="/bin/true"), config, env={"GH_TOKEN": "t"})
        assert provisioner.github_repo_env("o/a") == {
            "GH_HOST": "ghe.example.com",
            "SBXLOOP_GITHUB_API_URL": "https://ghe.example.com/api/v3",
            "GH_REPO": "o/a",
            "SBXLOOP_GITHUB_REPO": "o/a",
        }
        agent, github = provisioner.build_specs("r1", tmp_path, "o/a")
        # The github sandbox reaches the enterprise host and nothing on
        # github.com; the agent sandbox gains the enterprise host alongside
        # its Copilot hosts (the Copilot exchange stays a github.com affair).
        assert github.policy_allows == ["ghe.example.com"]
        assert "ghe.example.com" in agent.policy_allows
        assert [(s.host, s.env) for s in agent.secrets] == [
            ("api.github.com", "COPILOT_GITHUB_TOKEN")
        ]

    def test_dotcom_exports_no_host_and_keeps_the_storage_hosts(self, tmp_path: Path) -> None:
        provisioner = self._provisioner(tmp_path, [{"repo": "o/a"}], {"GH_TOKEN": "t"})
        assert "GH_HOST" not in provisioner.github_repo_env("o/a")
        _, github = provisioner.build_specs("r1", tmp_path, "o/a")
        assert github.policy_allows == [
            "api.github.com",
            "github.com",
            "uploads.github.com",
            "objects.githubusercontent.com",
        ]

    def test_single_repo_config_scopes_by_default(self, tmp_path: Path) -> None:
        provisioner = self._provisioner(
            tmp_path, [{"repo": "o/only", "token_env": "TOKEN_ONLY"}], {"TOKEN_ONLY": "tok"}
        )
        assert provisioner.github_repo_env(None)["SBXLOOP_GITHUB_REPO"] == "o/only"
        assert provisioner.gh_token(None) == "tok"


class TestGithubOpsSandboxProvisioning:
    """A full pair provisioned for a chosen repository: the github box gets
    that repo's token and remote, the agent box gets neither."""

    def test_pair_is_scoped_to_the_runs_repository(self, fake_sbx: Any, tmp_path: Path) -> None:
        from sbxloop.config import Config
        from sbxloop.sbx.cli import SbxCLI
        from sbxloop.sbx.provision import Provisioner

        config = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {
                    "repos": [
                        {"repo": "o/a", "token_env": "TOKEN_A"},
                        {"repo": "o/b", "token_env": "TOKEN_B"},
                    ]
                },
            }
        )
        provisioner = Provisioner(
            SbxCLI(binary=str(fake_sbx.binary)),
            config,
            env={
                "COPILOT_GITHUB_TOKEN": "copilot-token",
                "GH_TOKEN": "global-token",
                "TOKEN_A": "tok-a",
                "TOKEN_B": "tok-b",
            },
        )
        pair = provisioner.ensure_pair("r1", tmp_path / "ws", "o/b")
        try:
            github_env = (
                fake_sbx.sandbox_fs(pair.github.name) / "home/agent/.sbxloop/env.sh"
            ).read_text()
            assert "export SBXLOOP_GITHUB_REPO=o/b" in github_env
            assert "export GH_REPO=o/b" in github_env
            # ...and the repo's own token, not the daemon-wide one and not
            # the other repository's.
            secrets = [json.dumps(entry) for entry in fake_sbx.secrets()]
            github_secrets = [e for e in secrets if pair.github.name in e]
            assert github_secrets and all("tok-b" in e for e in github_secrets)
            assert not any("tok-a" in e for e in secrets)
            # Isolation invariant: the agent sandbox holds no GitHub token
            # (neither the global one nor either per-repo one) and is told
            # nothing about the run's repository.
            for entry in secrets:
                if pair.agent.name in entry:
                    assert "tok-a" not in entry
                    assert "tok-b" not in entry
                    assert "global-token" not in entry
            agent_env_file = fake_sbx.sandbox_fs(pair.agent.name) / "home/agent/.sbxloop/env.sh"
            agent_env = agent_env_file.read_text() if agent_env_file.exists() else ""
            # Only the Copilot token, under its own name: no GH_TOKEN /
            # GITHUB_TOKEN export and none of the GitHub token values.
            exported = {
                line.removeprefix("export ").split("=", 1)[0]
                for line in agent_env.splitlines()
                if line.startswith("export ")
            }
            assert exported <= {"COPILOT_GITHUB_TOKEN"}
            for forbidden in ("tok-a", "tok-b", "global-token"):
                assert forbidden not in agent_env
        finally:
            pair.agent.rm()
            pair.github.rm()
