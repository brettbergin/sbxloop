"""The entrygraph recipe selects inputs and keeps scans in the workload lane."""

from pathlib import Path

import pytest

from sbxloop.config import Config


def config(**kwargs: object) -> Config:
    return Config.model_validate(kwargs)


def test_default_selects_enabled_configured_repositories() -> None:
    from sbxloop.entrygraph import resolve_targets

    cfg = config(github={"repos": [{"repo": "org/one"}, {"repo": "org/two", "enabled": False}]})
    assert resolve_targets(cfg) == ["org/one"]


def test_url_matching_configured_repository_uses_its_credentials() -> None:
    from sbxloop.entrygraph import resolve_targets

    cfg = config(github={"repo": "org/one"})
    assert resolve_targets(cfg, url="https://github.com/org/one.git") == ["org/one"]
    assert resolve_targets(cfg, url="https://git.example.org/team/one.git") == [
        "https://git.example.org/team/one.git"
    ]


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/repo",
        "ssh://git@example.org/team/repo",
        "--upload-pack=oops",
        "https://token@example.org/team/repo",
        "https://example.org/repo?token=secret",
        "https://example.org/repo#fragment",
        "https://localhost/repo",
        "https://127.0.0.1/repo",
        "https://example.org/../repo",
        "https://example.org/%2e%2e/repo",
        "https://example.org/repo\nother",
        "https://example.org/",
        "https://example.org:99999/repo",
    ],
)
def test_unsafe_or_ambiguous_urls_are_refused(url: str) -> None:
    from sbxloop.entrygraph import resolve_targets

    with pytest.raises(ValueError):
        resolve_targets(config(), url=url)


def test_selectors_fail_closed() -> None:
    from sbxloop.entrygraph import resolve_targets

    cfg = config(github={"repo": "org/one"})
    with pytest.raises(ValueError, match="one"):
        resolve_targets(cfg, repo="org/one", url="https://example.org/repo")
    with pytest.raises(ValueError, match="configured"):
        resolve_targets(cfg, repo="org/missing")
    with pytest.raises(ValueError, match="configured"):
        resolve_targets(config())


def test_recipe_limits_run_to_scan_inputs_and_chat(tmp_path: Path) -> None:
    from sbxloop.entrygraph import scan_config, scan_task, stage_scanner

    cfg = config(
        home=tmp_path,
        github={"repo": "org/one", "repos": [{"repo": "org/one", "token_env": "ONE_TOKEN"}]},
        sandbox={"languages": ["node"], "setup_commands": ["do-build"], "env": {"MODE": "build"}},
        workloads=[{"name": "review", "publish": "hold", "sinks": ["pr"]}],
        workload={"default": "review"},
    )
    scan = scan_config(cfg, "org/one")
    profile = scan.workload_profile()
    assert profile is not None and profile.repo and profile.sinks == ["chat"]
    assert profile.publish == "hold" and not profile.credentials
    assert scan.github.find_repo("org/one").token_env == "ONE_TOKEN"
    assert scan.sandbox.languages == ["python"] and scan.sandbox.setup_commands == []
    assert scan.sandbox.env == {} and scan.registries == []
    task = scan_task("org/one")
    assert task.needs.repo == "org/one" and task.needs.sink == "chat"
    assert "--no-project" in task.description and "--url" not in task.description
    assert "--check" in task.verify_commands[0]
    staged = stage_scanner(scan, "rscan")
    assert staged.is_file() and staged.is_relative_to(scan.paths.run_workspace("rscan"))
    assert cfg.sandbox.languages == ["javascript"]


def test_public_url_recipe_has_no_github_capability(tmp_path: Path) -> None:
    from sbxloop.entrygraph import scan_config, scan_task

    cfg = config(
        home=tmp_path, github={"repo": "org/default"}, policy={"deny": ["blocked.example.org"]}
    )
    scan = scan_config(cfg, "https://git.example.org/team/repo.git")
    assert scan.github.repo is None and scan.github.repos == []
    task = scan_task("https://git.example.org/team/repo.git")
    assert task.needs.repo is None
    assert "git.example.org" in task.needs.hosts
    assert "--url https://git.example.org/team/repo.git" in task.description
    assert scan.policy.deny == cfg.policy.deny


def test_scan_config_rejects_disabled_or_removed_repository() -> None:
    from sbxloop.entrygraph import scan_config

    with pytest.raises(ValueError, match="configured"):
        scan_config(config(github={"repos": [{"repo": "org/one", "enabled": False}]}), "org/one")


@pytest.mark.parametrize("name", [".entrygraph", "entrygraph-report"])
def test_recipe_files_are_outside_the_configured_checkout(tmp_path: Path, name: str) -> None:
    from sbxloop.entrygraph import scan_config, scan_task, stage_scanner

    target = f"org/{name}"
    cfg = scan_config(config(home=tmp_path, github={"repo": target}), target)
    script = stage_scanner(cfg, "rlayout")
    checkout = cfg.paths.run_workspace("rlayout") / name
    assert not script.is_relative_to(checkout)
    task = scan_task(target)
    import shlex

    check = shlex.split(task.verify_commands[0])
    output = cfg.paths.run_workspace("rlayout") / check[check.index("--output") + 1]
    assert not output.is_relative_to(checkout)


def test_public_repository_tree_is_not_a_result_artifact() -> None:
    from sbxloop.entrygraph import scan_config

    cfg = scan_config(config(), "https://git.example.org/team/repo.git")
    assert "repository" in cfg.artifacts.exclude
    assert ".entrygraph" in cfg.artifacts.exclude


def test_daemon_scan_does_not_refresh_an_unrelated_host_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sbxloop.daemon.model import WorkItem
    from tests.unit.test_daemon_loop import Harness

    h = Harness(tmp_path)
    item = WorkItem(
        item_id="chat:scan",
        source_key="scan",
        title="Scan repository",
        kind="workload",
        entrygraph_target="https://git.example.org/team/repo.git",
    )
    h.dstore.upsert_new(item, h.clock())
    h.outcomes = ["completed"]
    refreshed = []
    monkeypatch.setattr(h.loop, "_refresh_workspace", refreshed.append)
    assert h.loop.tick().outcome == "done"
    assert refreshed == []


def test_default_runner_stages_scanner_and_seeds_workload(tmp_path: Path) -> None:
    from unittest.mock import Mock

    from sbxloop.daemon.loop import RunHandle
    from sbxloop.daemon.model import WorkItem
    from sbxloop.events import EventBus
    from tests.unit.test_daemon_loop import Harness

    h = Harness(tmp_path)
    item = WorkItem(
        item_id="chat:scan",
        source_key="scan",
        title="Scan repository",
        kind="workload",
        entrygraph_target="o/r",
        repo="o/r",
    )
    cfg = h.loop._item_config(item)
    engine = Mock()
    bus = EventBus()
    h.loop._current = RunHandle(item, "rscan", engine, bus)
    h.loop._default_runner(item, cfg, "rscan", bus, False)
    call = engine.start.call_args
    assert call.kwargs["kind"] == "workload" and call.kwargs["repo"] == "o/r"
    assert call.kwargs["tasks"][0].needs.repo == "o/r"
    assert engine.config.workload_profile().name == "entrygraph"
    assert (cfg.paths.run_workspace("rscan") / ".entrygraph/scan.py").is_file()
    h.loop._default_runner(item, cfg, "rscan", bus, True)
    engine.resume.assert_called_once_with("rscan", release_provider_hold=False)
    assert engine.start.call_count == 1
