"""The entrygraph recipe selects inputs and seeds a `tool` run — no agent in it."""

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
    from sbxloop.entrygraph import stage_scanner, tool_config, tool_task

    cfg = config(
        home=tmp_path,
        github={"repo": "org/one", "repos": [{"repo": "org/one", "token_env": "ONE_TOKEN"}]},
        sandbox={"languages": ["node"], "setup_commands": ["do-build"], "env": {"MODE": "build"}},
        workloads=[{"name": "review", "publish": "hold", "sinks": ["pr"]}],
        workload={"default": "review"},
    )
    scan = tool_config(cfg, "org/one")
    # No profile is synthesized: a tool run's bounds are its task's needs.
    assert scan.workloads == cfg.workloads and scan.workload == cfg.workload
    assert scan.github.find_repo("org/one").token_env == "ONE_TOKEN"
    assert scan.sandbox.languages == ["python"] and scan.sandbox.setup_commands == []
    assert scan.sandbox.env == {} and scan.registries == []
    task = tool_task(scan, "org/one")
    assert task.needs.repo == "org/one" and task.needs.sink == "chat"
    assert task.command is not None
    assert "--no-project" in task.command and "--url" not in task.command
    assert "--check" in task.verify_commands[0]
    assert task.result_files == ["entrygraph-report/report.md", "entrygraph-report/report.json"]
    # The description is for a person reading the roster, never a brief.
    assert "Run exactly" not in task.description
    staged = stage_scanner(scan, "rscan", "org/one")
    assert staged.is_file() and staged.is_relative_to(scan.paths.run_workspace("rscan"))
    assert cfg.sandbox.languages == ["javascript"]


def test_public_url_recipe_has_no_github_capability(tmp_path: Path) -> None:
    from sbxloop.entrygraph import tool_config, tool_task

    cfg = config(
        home=tmp_path, github={"repo": "org/default"}, policy={"deny": ["blocked.example.org"]}
    )
    scan = tool_config(cfg, "https://git.example.org/team/repo.git")
    assert scan.github.repo is None and scan.github.repos == []
    task = tool_task(scan, "https://git.example.org/team/repo.git")
    assert task.needs.repo is None
    assert "git.example.org" in task.needs.hosts
    assert task.command is not None
    assert "--url https://git.example.org/team/repo.git" in task.command
    assert scan.policy.deny == cfg.policy.deny


def test_tool_config_rejects_disabled_or_removed_repository() -> None:
    from sbxloop.entrygraph import tool_config

    with pytest.raises(ValueError, match="configured"):
        tool_config(config(github={"repos": [{"repo": "org/one", "enabled": False}]}), "org/one")


@pytest.mark.parametrize("name", [".entrygraph", "entrygraph-report"])
def test_recipe_files_are_outside_the_configured_checkout(tmp_path: Path, name: str) -> None:
    from sbxloop.entrygraph import stage_scanner, tool_config, tool_task

    target = f"org/{name}"
    cfg = tool_config(config(home=tmp_path, github={"repo": target}), target)
    script = stage_scanner(cfg, "rlayout", target)
    checkout = cfg.paths.run_workspace("rlayout") / name
    assert not script.is_relative_to(checkout)
    task = tool_task(cfg, target)
    import shlex

    check = shlex.split(task.verify_commands[0])
    output = cfg.paths.run_workspace("rlayout") / check[check.index("--output") + 1]
    assert not output.is_relative_to(checkout)


def test_public_repository_tree_is_not_a_result_artifact() -> None:
    from sbxloop.entrygraph import tool_config

    cfg = tool_config(config(), "https://git.example.org/team/repo.git")
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
        kind="tool",
        recipe="entrygraph",
        recipe_target="https://git.example.org/team/repo.git",
    )
    h.dstore.upsert_new(item, h.clock())
    h.outcomes = ["completed"]
    refreshed = []
    monkeypatch.setattr(h.loop, "_refresh_workspace", refreshed.append)
    assert h.loop.tick().outcome == "done"
    assert refreshed == []


def test_default_runner_stages_scanner_and_seeds_a_tool_run(tmp_path: Path) -> None:
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
        kind="tool",
        recipe="entrygraph",
        recipe_target="o/r",
        repo="o/r",
    )
    cfg = h.loop._item_config(item)
    engine = Mock()
    bus = EventBus()
    h.loop._current = RunHandle(item, "rscan", engine, bus)
    h.loop._default_runner(item, cfg, "rscan", bus, False)
    call = engine.start.call_args
    assert call.kwargs["kind"] == "tool" and call.kwargs["repo"] == "o/r"
    (seeded,) = call.kwargs["tasks"]
    assert seeded.needs.repo == "o/r" and seeded.command is not None
    assert "profile" not in call.kwargs
    assert (cfg.paths.run_workspace("rscan") / ".entrygraph/scan.py").is_file()
    h.loop._default_runner(item, cfg, "rscan", bus, True)
    engine.resume.assert_called_once_with("rscan", release_provider_hold=False)
    assert engine.start.call_count == 1


def test_the_analyzer_is_pinned_in_one_place() -> None:
    """The scanner refuses a runtime that is not its own version, so two
    pins could only ever disagree at run time, on a run that then fails."""
    from sbxloop.data.entrygraph_scan import ENTRYGRAPH_VERSION
    from sbxloop.entrygraph import RUNTIME

    assert f"--with entrygraph=={ENTRYGRAPH_VERSION} " in RUNTIME


def test_the_scan_runtime_installs_wheels_and_reaches_no_code_host() -> None:
    """The pinned analyzer and grammar pack both publish wheels. Building
    from source would reach hosts this recipe does not grant, and fail deep
    inside a compile rather than at resolution."""
    from sbxloop.entrygraph import RUNTIME, RUNTIME_HOSTS, tool_task

    assert "--no-build" in RUNTIME
    assert set(RUNTIME_HOSTS) == {"pypi.org", "files.pythonhosted.org"}
    task = tool_task(config(github={"repo": "org/one"}), "org/one")
    assert task.needs.hosts == ["pypi.org", "files.pythonhosted.org"]


def test_extra_hosts_reach_the_task_declaration() -> None:
    """The declared hosts are the run's whole egress grant."""
    from sbxloop.entrygraph import tool_config, tool_task

    cfg = config(
        github={"repo": "org/one"},
        entrygraph={"extra_hosts": ["Mirror.Example.Org"]},
    )
    scan = tool_config(cfg, "org/one")
    assert tool_task(scan, "org/one").needs.hosts == [
        "pypi.org",
        "files.pythonhosted.org",
        "mirror.example.org",
    ]


def test_public_urls_can_be_narrowed_to_configured_repositories() -> None:
    from sbxloop.entrygraph import resolve_targets

    cfg = config(
        github={"repo": "org/one"},
        entrygraph={"allow_public_urls": False},
    )
    # the configured repository is still reachable by its own URL
    assert resolve_targets(cfg, url="https://github.com/org/one.git") == ["org/one"]
    with pytest.raises(ValueError, match="allow_public_urls"):
        resolve_targets(cfg, url="https://git.example.org/team/repo.git")


def test_the_registry_is_how_the_daemon_reaches_a_recipe() -> None:
    from sbxloop import entrygraph
    from sbxloop.errors import ConfigError
    from sbxloop.recipes import get_recipe

    recipe = get_recipe("entrygraph")
    assert recipe.config is entrygraph.tool_config
    assert recipe.stage is entrygraph.stage_scanner
    assert recipe.task is entrygraph.tool_task
    with pytest.raises(ConfigError, match="entrygraph"):
        get_recipe("no-such-recipe")


def test_no_recipe_command_relies_on_a_bare_python() -> None:
    """The sandbox has `python3` and uv's managed interpreter, never a
    `python` alias: a check written as `python …` dies with exit 127 in
    the raw shell before the scanner runs (seen in the field). The scan
    command is exempt because `uv run … python` resolves the name itself."""
    import shlex

    from sbxloop.entrygraph import tool_task

    for target in ("org/one", "https://git.example.org/team/repo.git"):
        task = tool_task(config(github={"repo": "org/one"}), target)
        assert task.command is not None
        for command in task.verify_commands:
            assert shlex.split(command)[0] == "python3", command
        assert not task.command.startswith("python ")
