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
                vcs={"kind": "gitlab", "api_url": "https://gl.example/api/v4"},
                github={"repos": [{"repo": "o/a"}, {"repo": "o/b", "kind": "gitea"}]},
            )
        )
        assert [(r.name, r.ok, r.hard) for r in rows] == [
            ("vcs backend gitlab", True, False),
            ("vcs backend gitea", False, True),
        ]
        assert "not implemented yet" in rows[1].detail
        assert "github, gitlab" in rows[1].detail

    def test_gitlab_lists_its_verified_matrix_and_the_roles_still_missing(self) -> None:
        (row,) = vcs_backend_checks(
            cfg(
                vcs={"kind": "gitlab", "api_url": "https://gl.example/api/v4"},
                github={"repo": "o/r"},
            )
        )
        assert row.ok and not row.hard
        assert "supported: review_threads" in row.detail
        assert "unsupported: request_changes_review" in row.detail
        assert "unknown: merge_queue" in row.detail and "merge trains" in row.detail
        assert "not implemented" not in row.detail

    def test_a_forge_without_an_api_root_fails_its_row(self) -> None:
        (row,) = vcs_backend_checks(cfg(vcs={"kind": "gitlab"}, github={"repo": "o/r"}))
        assert not row.ok and row.hard and "[vcs] api_url is not set" in row.detail

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


class TestTokenRow:
    """A repository on another forge is judged on its own token variable,
    by the name the configuration resolves, never on GH_TOKEN."""

    def test_a_gitlab_repository_needs_its_own_variable(self) -> None:
        config = cfg(vcs={"kind": "gitlab"}, github={"repos": [{"repo": "acme/alpha"}]})
        (row,) = repo_checks(config, {"GH_TOKEN": "tok"})
        assert not row.ok and "token_env GITLAB_TOKEN is not set on the host" in row.detail
        (row,) = repo_checks(config, {"GITLAB_TOKEN": "glpat"})
        assert row.ok and "token from GITLAB_TOKEN" in row.detail

    def test_the_configured_name_is_the_one_checked(self) -> None:
        config = cfg(
            vcs={"kind": "gitea", "token_env": "GT_MAIN"},
            github={"repos": [{"repo": "acme/alpha"}]},
        )
        (row,) = repo_checks(config, {"GITEA_TOKEN": "x"})
        assert not row.ok and "GT_MAIN is not set" in row.detail
        (row,) = repo_checks(config, {"GT_MAIN": "x"})
        assert row.ok and "token from GT_MAIN" in row.detail

    def test_a_github_repository_is_unchanged(self) -> None:
        config = cfg(github={"repos": [{"repo": "acme/alpha"}]})
        (row,) = repo_checks(config, {"GH_TOKEN": "tok"})
        assert row.ok and "token from GH_TOKEN" in row.detail
