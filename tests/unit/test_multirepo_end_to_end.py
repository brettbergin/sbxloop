"""Multi-repo end to end: config -> discovery -> dispatch -> report.

The other multi-repo suites each test one seam. This one wires the *real*
pieces together — a parsed ``sbxloop.toml``, :func:`build_github_source`
over fake GitHub ops, and a real :class:`DaemonLoop` — and checks that an
issue labelled in repository B produces a run configured for B and reports
back to B, while a single-repo config walks exactly the same path with the
legacy unqualified ids it always used.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, cast

import pytest

from sbxloop.config import Config
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.daemon.sources import GitHubLabels, build_github_source
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import RunResult
from sbxloop.engine.store import StateStore
from sbxloop.events import EventBus

from .test_daemon_loop import PR_URL, RecordingFrontend
from .test_daemon_sources import RecordingOps, issue
from .test_daemon_sources_multirepo import RouterOps

LABELS = GitHubLabels("sbxloop:run", "sbxloop:in-progress", "sbxloop:failed")

MULTI_TOML = """
state_dir = "{state_dir}"

[github]
deliver_base = "main"

[[github.repos]]
repo = "o/a"

[[github.repos]]
repo = "o/b"
deliver_base = "develop"
"""

SINGLE_TOML = """
state_dir = "{state_dir}"

[github]
repo = "o/a"
deliver_base = "main"
"""


def _config(tmp_path: Path, template: str) -> Config:
    path = tmp_path / "sbxloop.toml"
    path.write_text(template.format(state_dir=(tmp_path / "state").as_posix()))
    return Config.model_validate(tomllib.loads(path.read_text()))


class RecordingRunner:
    """Stands in for the engine: records the config each dispatch handed it
    and finishes the run merged with a PR on it."""

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.seen: list[tuple[WorkItem, Config]] = []

    def __call__(
        self, item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
    ) -> RunResult:
        self.seen.append((item, cfg))
        self.store.create_run(run_id, item.title)
        self.store.set_run_pr(
            run_id, number=9, url=PR_URL, branch=f"sbxloop/{run_id}", head_sha="abc"
        )
        self.store.set_run_state(run_id, "merged")
        return RunResult(run_id=run_id, state="merged", pr_number=9, pr_url=PR_URL)


class Wiring:
    """A DaemonLoop over real stores, the real GitHub source and fake ops."""

    def __init__(self, tmp_path: Path, config: Config, router: RouterOps) -> None:
        self.config = config
        self.router = router
        self.store = StateStore(config.state_dir / "state.db")
        self.dstore = DaemonStore(config.state_dir / "state.db")
        self.runner = RecordingRunner(self.store)
        self.frontend = RecordingFrontend()
        self.source = build_github_source(
            lambda: cast(Any, router), config.github.repo_list(), LABELS, host="db"
        )
        self.loop = DaemonLoop(
            config,
            store=self.store,
            dstore=self.dstore,
            source=self.source,
            runner=self.runner,
            frontend=self.frontend,
        )


def _router() -> RouterOps:
    return RouterOps(
        {
            "o/a": RecordingOps({"4": issue(4, "sbxloop:run"), "5": issue(5, "other")}),
            "o/b": RecordingOps({"7": issue(7, "sbxloop:run")}),
        }
    )


@pytest.fixture
def multi(tmp_path: Path) -> Wiring:
    return Wiring(tmp_path, _config(tmp_path, MULTI_TOML), _router())


class TestMultiRepoDaemon:
    def test_discovery_spans_both_repos_with_qualified_ids(self, multi: Wiring) -> None:
        items = multi.source.poll()
        assert multi.router.searched == ["o/a", "o/b"]
        assert [i.item_id for i in items] == ["gh:o/a:issue:4", "gh:o/b:issue:7"]
        assert [i.repo for i in items] == ["o/a", "o/b"]

    def test_a_run_is_configured_for_the_repo_its_issue_came_from(self, multi: Wiring) -> None:
        # Two ticks: one item each, dispatched in discovery order.
        assert multi.loop.tick().dispatched == "gh:o/a:issue:4"
        assert multi.loop.tick().dispatched == "gh:o/b:issue:7"

        by_repo = {item.repo: cfg for item, cfg in multi.runner.seen}
        assert set(by_repo) == {"o/a", "o/b"}
        # Each run's config is narrowed to its own repository, and carries
        # that repository's base branch and its own issue number to close.
        assert by_repo["o/a"].github.repo == "o/a"
        assert by_repo["o/a"].github.deliver_base == "main"
        assert by_repo["o/a"].github.deliver_closes == 4
        assert by_repo["o/b"].github.repo == "o/b"
        assert by_repo["o/b"].github.deliver_base == "develop"
        assert by_repo["o/b"].github.deliver_closes == 7

    def test_claims_comments_and_closes_land_on_the_owning_repo(self, multi: Wiring) -> None:
        multi.loop.tick()
        multi.loop.tick()
        a, b = multi.router.per_repo["o/a"], multi.router.per_repo["o/b"]
        # Every write each repo saw named that repo's own path, and each
        # issue was closed in its own repository.
        assert all(p.startswith("/repos/o/a/") for _, p, _ in a.raw_calls)
        assert all(p.startswith("/repos/o/b/") for _, p, _ in b.raw_calls)
        closed = {"state": "closed", "state_reason": "completed"}
        assert ("PATCH", "/repos/o/a/issues/4", closed) in a.raw_calls
        assert ("PATCH", "/repos/o/b/issues/7", closed) in b.raw_calls
        # The merge comment went to the right issue on each side.
        assert [n for n, _ in a.comments] == [4, 4, 4]
        assert [n for n, _ in b.comments] == [7, 7, 7]

    def test_both_items_settle_done_and_the_queue_drains(self, multi: Wiring) -> None:
        multi.loop.tick()
        multi.loop.tick()
        for item_id in ("gh:o/a:issue:4", "gh:o/b:issue:7"):
            row = multi.dstore.get(item_id)
            assert row is not None and row.state == "done" and row.pending_report is None
        assert multi.loop.tick().idle_reason == "no_work"
        reports = [r for _, r in multi.frontend.finished]
        assert all(isinstance(r, RunReport) and r.succeeded for r in reports)

    def test_one_unreachable_repo_does_not_blank_the_other(self, multi: Wiring) -> None:
        multi.router.fail_search.add("o/a")
        assert multi.loop.tick().dispatched == "gh:o/b:issue:7"
        assert multi.runner.seen[0][0].repo == "o/b"


class TestSingleRepoBackCompat:
    """The legacy ``[github] repo`` config walks the same path unchanged."""

    @pytest.fixture
    def single(self, tmp_path: Path) -> Wiring:
        return Wiring(tmp_path, _config(tmp_path, SINGLE_TOML), _router())

    def test_config_normalises_to_one_entry(self, single: Wiring) -> None:
        assert [r.repo for r in single.config.github.repo_list()] == ["o/a"]
        assert single.config.github.repo == "o/a"

    def test_ids_stay_unqualified_and_only_that_repo_is_polled(self, single: Wiring) -> None:
        items = single.source.poll()
        assert [i.item_id for i in items] == ["gh:issue:4"]
        assert single.router.searched == ["o/a"]

    def test_a_run_merges_and_closes_the_issue_as_before(self, single: Wiring) -> None:
        assert single.loop.tick().dispatched == "gh:issue:4"
        _item, cfg = single.runner.seen[0]
        assert cfg.github.repo == "o/a" and cfg.github.deliver_base == "main"
        row = single.dstore.get("gh:issue:4")
        assert row is not None and row.state == "done"
        assert (
            "PATCH",
            "/repos/o/a/issues/4",
            {"state": "closed", "state_reason": "completed"},
        ) in single.router.per_repo["o/a"].raw_calls
        assert single.router.per_repo["o/b"].raw_calls == []

    def test_a_legacy_persisted_item_without_a_repo_still_runs(self, single: Wiring) -> None:
        legacy = WorkItem(item_id="gh:4", source_key="4", title="old", url="https://x/issues/4")
        single.dstore.upsert_new(legacy, now=1.0)
        assert single.loop.tick().dispatched == "gh:issue:4"
        assert single.runner.seen[0][1].github.repo == "o/a"


class TestSingleToMultiUpgrade:
    """The migration the docs prescribe: a store written by a single-repo
    daemon (rows with no repository) meeting a config that now lists two.

    There is no sole owner to attribute those rows to, so the daemon drops
    the non-terminal ones at startup and lets discovery re-create them,
    repo-qualified — otherwise the issue would be queued twice (a repo-less
    row does not dedup against a qualified item) and the legacy row's own
    run could be routed to whichever repository is listed first.
    """

    @pytest.fixture
    def upgraded(self, tmp_path: Path) -> Wiring:
        config = _config(tmp_path, MULTI_TOML)
        # Pre-upgrade state: the old daemon polled o/b and queued issue 7
        # with an unqualified id and no repository.
        pre = DaemonStore(config.state_dir / "state.db")
        pre.upsert_new(
            WorkItem(item_id="gh:7", source_key="7", title="old", url="https://x/issues/7"),
            now=1.0,
        )
        pre.close()
        return Wiring(tmp_path, config, _router())

    def _start(self, wiring: Wiring) -> int:
        """What the daemon entrypoint does at startup for repo-less rows."""
        configured = wiring.config.github.repo_list()
        if len(configured) == 1:
            return wiring.dstore.backfill_repo(configured[0].repo)
        repos = [r.repo for r in configured]
        wiring.dstore.attribute_repoless(repos)
        dropped = wiring.dstore.drop_repoless()
        wiring.dstore.strand_repoless("no repo", now=1.5)
        return dropped

    def test_startup_drops_the_unattributable_row(self, upgraded: Wiring) -> None:
        assert self._start(upgraded) == 1
        assert upgraded.dstore.get("gh:issue:7") is None

    def test_startup_settles_an_in_flight_row_instead_of_dropping_it(
        self, upgraded: Wiring
    ) -> None:
        """A row claimed before the upgrade cannot be rediscovered (its
        trigger label is gone), so it is failed and kept, not deleted."""
        upgraded.dstore.mark_claimed("gh:issue:7", 1.1)
        upgraded.dstore.mark_running("gh:issue:7", "r1", 1.2)
        assert self._start(upgraded) == 0
        row = upgraded.dstore.get("gh:issue:7")
        assert row is not None and row.state == "failed" and row.run_id == "r1"

    def test_one_item_per_issue_with_the_right_repo(self, upgraded: Wiring) -> None:
        self._start(upgraded)
        items = upgraded.source.poll()
        for item in items:
            upgraded.dstore.upsert_new(item, now=2.0)
        queued = upgraded.dstore.items()
        # Exactly one row per GitHub issue, each carrying its own repo.
        assert sorted((i.source_key, i.repo) for i in queued) == [("4", "o/a"), ("7", "o/b")]
        assert sorted(i.item_id for i in queued) == ["gh:o/a:issue:4", "gh:o/b:issue:7"]

    def test_the_rediscovered_run_targets_the_repo_the_issue_lives_in(
        self, upgraded: Wiring
    ) -> None:
        self._start(upgraded)
        upgraded.loop.tick()
        upgraded.loop.tick()
        by_repo = {item.source_key: cfg.github.repo for item, cfg in upgraded.runner.seen}
        assert by_repo == {"4": "o/a", "7": "o/b"}
        # No spurious claim failure: nothing was dispatched twice.
        assert len(upgraded.runner.seen) == 2
        assert upgraded.loop.tick().idle_reason == "no_work"

    def test_terminal_repoless_rows_are_kept_as_history(self, upgraded: Wiring) -> None:
        upgraded.dstore.mark_done("gh:issue:7", now=2.0)
        assert self._start(upgraded) == 0
        row = upgraded.dstore.get("gh:issue:7")
        assert row is not None and row.state == "done"
