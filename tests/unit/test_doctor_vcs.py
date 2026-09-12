"""The doctor's backend rows and credential note (#1013).

An ``UNKNOWN`` capability must be visible before a run hits it, and a
``[vcs] kind`` no backend answers must fail the doctor before the run
fails closed at its first operation. Each repository's row says what kind
of credential its github box holds, and how long that lives."""

from __future__ import annotations

from sbxloop.cli.doctor import RepoProbe, repo_checks, vcs_backend_checks
from sbxloop.config import Config
from sbxloop.vcs.github.ops import GithubOps
from sbxloop.vcs.protocol import CAPABILITIES


def cfg(**doc: object) -> Config:
    return Config.model_validate(doc)


class TestBackendRows:
    def test_github_lists_every_capability_by_state(self) -> None:
        (row,) = vcs_backend_checks(cfg(github={"repo": "o/r"}))
        assert row.name == "vcs backend github" and row.ok and not row.hard
        for capability in CAPABILITIES:
            assert capability in row.detail
        assert row.detail.startswith("supported: ")
        assert "unknown: signed_api_commits" in row.detail
        assert "depend on the credential" in row.detail

    def test_the_report_is_the_backends_own(self) -> None:
        (row,) = vcs_backend_checks(cfg())
        for capability, state in GithubOps.CAPABILITIES.items():
            assert f"{state}: " in row.detail and capability in row.detail

    def test_a_kind_no_backend_answers_fails_the_row(self) -> None:
        rows = vcs_backend_checks(
            cfg(
                vcs={"kind": "gitlab"},
                github={"repos": [{"repo": "o/a"}, {"repo": "o/b", "kind": "gitea"}]},
            )
        )
        assert [(r.name, r.ok, r.hard) for r in rows] == [
            ("vcs backend gitlab", False, True),
            ("vcs backend gitea", False, True),
        ]
        assert "not implemented yet" in rows[0].detail
        assert '"github" answers a run today' in rows[0].detail

    def test_one_row_per_forge_not_per_repository(self) -> None:
        rows = vcs_backend_checks(cfg(github={"repos": [{"repo": "o/a"}, {"repo": "o/b"}]}))
        assert [r.name for r in rows] == ["vcs backend github"]


class TestCredentialNote:
    def test_the_repository_row_names_the_credential_kind(self) -> None:
        config = cfg(github={"repos": [{"repo": "acme/alpha"}]})
        rows = repo_checks(
            config,
            {"GH_TOKEN": "tok"},
            probe=lambda _e: RepoProbe(
                reachable=True,
                credential="fine-grained PAT (long-lived; its expiry is not readable by the token)",
            ),
        )
        (row,) = rows
        assert row.name == "github repo acme/alpha" and row.ok
        assert "credential: fine-grained PAT (long-lived" in row.detail

    def test_an_undetermined_credential_adds_nothing(self) -> None:
        config = cfg(github={"repos": [{"repo": "acme/alpha"}]})
        (row,) = repo_checks(
            config, {"GH_TOKEN": "tok"}, probe=lambda _e: RepoProbe(reachable=True)
        )
        assert "credential:" not in row.detail
