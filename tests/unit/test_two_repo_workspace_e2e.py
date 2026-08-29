"""Two-repo end to end: each run's workspace origin is its own repository.

The field failure this closes (#526): a daemon configured with two
``[[github.repos]]`` entries and a single legacy ``[sandbox] workspace``
(a checkout of the *first* repo) dispatched a run for the second repo into
a clone of the first repo's tree. Nothing between config parsing and
provisioning noticed.

These tests wire the real pieces — a parsed ``sbxloop.toml``, the real
:class:`DaemonLoop` over the real GitHub source and fake ops, and the real
:class:`Provisioner` workspace resolution — over two local git fixtures, and
assert on the one property that was violated in the field: the ``origin`` of
the tree a run works in names the repository the run's work item came from.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
from git import Repo

from sbxloop import hostgit
from sbxloop.config import Config
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.sources import GitHubLabels, build_github_source
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import RunResult
from sbxloop.engine.store import StateStore
from sbxloop.errors import ProvisionError
from sbxloop.events import EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner
from tests.conftest import FakeSbx

from .test_daemon_loop import PR_URL, RecordingFrontend
from .test_daemon_sources import RecordingOps, issue
from .test_daemon_sources_multirepo import RouterOps
from .test_provision import TOKENS

LABELS = GitHubLabels("sbxloop:run", "sbxloop:in-progress", "sbxloop:failed")

# Two repositories, each pointed at its own host checkout: the shape the
# migration note in the README asks operators to move to.
PER_REPO_TOML = """
state_dir = "{state_dir}"

[sandbox]
workspace_isolation = "clone"

[[github.repos]]
repo = "o/a"
workspace = "{a}"

[[github.repos]]
repo = "o/b"
workspace = "{b}"
"""

# The exact field configuration that failed: one daemon-wide workspace (a
# checkout of o/a) and two enabled repos.
FIELD_TOML = """
state_dir = "{state_dir}"

[sandbox]
workspace = "{a}"
workspace_isolation = "clone"

[[github.repos]]
repo = "o/a"

[[github.repos]]
repo = "o/b"
"""


def _checkout(path: Path, repo: str) -> Path:
    """A local git checkout whose ``origin`` names ``repo`` on GitHub."""
    path.mkdir(parents=True, exist_ok=True)
    git_repo = Repo.init(path)
    (path / "MARKER").write_text(f"{repo}\n")
    git_repo.index.add(["MARKER"])
    git_repo.index.commit(f"init {repo}")
    git_repo.create_remote("origin", f"https://github.com/{repo}.git")
    return path


def _config(tmp_path: Path, template: str) -> Config:
    a = _checkout(tmp_path / "trees" / "a", "o/a")
    b = _checkout(tmp_path / "trees" / "b", "o/b")
    path = tmp_path / "sbxloop.toml"
    path.write_text(
        template.format(
            state_dir=(tmp_path / "state").as_posix(),
            a=a.as_posix(),
            b=b.as_posix(),
        )
    )
    return Config.model_validate(tomllib.loads(path.read_text()))


class ProvisioningRunner:
    """Stands in for the engine, but does the one thing under test for
    real: resolve the run's workspace through the real Provisioner, using
    the per-run config the daemon handed it."""

    def __init__(self, store: StateStore, fake_sbx: FakeSbx) -> None:
        self.store = store
        self.fake_sbx = fake_sbx
        self.workspaces: dict[str, Path] = {}
        self.errors: dict[str, ProvisionError] = {}
        self.seen: list[tuple[WorkItem, Config]] = []

    def __call__(
        self, item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
    ) -> RunResult:
        self.seen.append((item, cfg))
        self.store.create_run(run_id, item.title)
        provisioner = Provisioner(SbxCLI(binary=str(self.fake_sbx.binary)), cfg, env=TOKENS)
        repo = cast(str, item.repo)
        try:
            self.workspaces[repo] = provisioner._resolve_workspace(run_id, repo)
        except ProvisionError as exc:
            self.errors[repo] = exc
            self.store.set_run_state(run_id, "failed")
            return RunResult(run_id=run_id, state="failed")
        self.store.set_run_pr(
            run_id, number=9, url=PR_URL, branch=f"sbxloop/{run_id}", head_sha="abc"
        )
        self.store.set_run_state(run_id, "merged")
        return RunResult(run_id=run_id, state="merged", pr_number=9, pr_url=PR_URL)


class Wiring:
    def __init__(self, tmp_path: Path, config: Config, fake_sbx: FakeSbx) -> None:
        self.config = config
        self.router = RouterOps(
            {
                "o/a": RecordingOps({"4": issue(4, "sbxloop:run")}),
                "o/b": RecordingOps({"7": issue(7, "sbxloop:run")}),
            }
        )
        self.store = StateStore(config.state_dir / "state.db")
        self.dstore = DaemonStore(config.state_dir / "state.db")
        self.runner = ProvisioningRunner(self.store, fake_sbx)
        self.loop = DaemonLoop(
            config,
            store=self.store,
            dstore=self.dstore,
            source=build_github_source(
                lambda: cast(Any, self.router), config.github.repo_list(), LABELS, host="db"
            ),
            runner=self.runner,
            frontend=RecordingFrontend(),
        )

    def run_both(self) -> None:
        self.loop.tick()
        self.loop.tick()

    @property
    def errors(self) -> dict[str, ProvisionError]:
        return self.runner.errors


@pytest.fixture
def per_repo(tmp_path: Path, fake_sbx: FakeSbx) -> Wiring:
    return Wiring(tmp_path, _config(tmp_path, PER_REPO_TOML), fake_sbx)


@pytest.fixture
def field(tmp_path: Path, fake_sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch) -> Wiring:
    # o/b has no workspace of its own here, so resolution falls through to
    # the clone-from-remote mode; o/b is not a real repository, so stand in
    # for the network with the failure that mode would report.
    def unreachable(url: str, target: Path, branch: str, *, existing: bool = False) -> str:
        raise ProvisionError(f"cloning {url} failed: repository not found")

    monkeypatch.setattr(hostgit, "clone_from_remote", unreachable)
    return Wiring(tmp_path, _config(tmp_path, FIELD_TOML), fake_sbx)


class TestTwoRepoWorkspaces:
    """Each repo entry has its own workspace; each run gets its own tree."""

    def test_each_runs_workspace_origin_is_its_own_repo(self, per_repo: Wiring) -> None:
        per_repo.run_both()
        assert not per_repo.errors
        for repo in ("o/a", "o/b"):
            workspace = per_repo.runner.workspaces[repo]
            assert hostgit.origin_matches_repo(workspace, repo) is True

    def test_the_second_repos_run_never_gets_the_firsts_tree(self, per_repo: Wiring) -> None:
        per_repo.run_both()
        b = per_repo.runner.workspaces["o/b"]
        assert (b / "MARKER").read_text() == "o/b\n"
        assert hostgit.origin_matches_repo(b, "o/a") is False
        # ...and the two runs worked in different trees entirely.
        assert b != per_repo.runner.workspaces["o/a"]

    def test_each_run_is_isolated_in_its_own_run_dir(self, per_repo: Wiring) -> None:
        per_repo.run_both()
        runs = per_repo.config.state_dir / "runs"
        for repo, workspace in per_repo.runner.workspaces.items():
            assert runs in workspace.parents, repo
            # A clone, not the source checkout itself.
            assert workspace != per_repo.config.workspace_for_repo(repo)

    def test_the_config_each_run_saw_carries_its_own_workspace(self, per_repo: Wiring) -> None:
        per_repo.run_both()
        by_repo = {cast(str, item.repo): cfg for item, cfg in per_repo.runner.seen}
        for repo, cfg in by_repo.items():
            assert cfg.github.repo == repo
            entry = cfg.github.repo_list()[0]
            assert entry.workspace is not None
            assert hostgit.origin_matches_repo(entry.workspace, repo) is True


class TestFieldConfigurationIsRefused:
    """Regression for run ``rvnbn7n2m``: one ``[sandbox] workspace`` (a
    checkout of o/a) plus two enabled repos. The o/b run must be refused,
    not provisioned from o/a's tree."""

    def test_the_run_for_the_other_repo_is_refused(self, field: Wiring) -> None:
        field.run_both()
        assert "o/b" in field.runner.errors
        message = str(field.runner.errors["o/b"])
        assert "o/b" in message
        # It failed for a reason an operator can act on, and it did not
        # quietly succeed against the wrong checkout.
        assert "workspace" in message
        assert "o/b" not in field.runner.workspaces

    def test_no_tree_was_provisioned_for_the_refused_run(self, field: Wiring) -> None:
        field.run_both()
        cloned = [
            path
            for path in (field.config.state_dir / "runs").glob("*/workspace")
            if (path / ".git").exists()
        ]
        # Only o/a's run — whose workspace legitimately is that checkout —
        # produced a tree, and it is o/a's.
        for path in cloned:
            assert hostgit.origin_matches_repo(path, "o/a") is True
        assert len(cloned) <= 1

    def test_doctor_and_daemon_start_fail_on_the_same_configuration(self, field: Wiring) -> None:
        from sbxloop.cli.doctor import workspace_origin_checks, workspace_origin_mismatches

        mismatches = workspace_origin_mismatches(field.config)
        assert [m.repo for m in mismatches] == ["o/b"]
        assert "o/a" in mismatches[0].message
        checks = workspace_origin_checks(field.config)
        assert checks and not any(check.ok for check in checks)
